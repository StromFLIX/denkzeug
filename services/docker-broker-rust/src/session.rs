//! Linux filesystem locks shared with the Python backend. Claim files are not
//! in the agent-visible subpath and are never deleted or replaced on release.
use std::{fs, io, os::fd::OwnedFd, path::PathBuf};

use rustix::{
    fs::{self as unix, AtFlags, Dir, FileType, FlockOperation, Mode, OFlags},
    io::Errno,
    process::{Gid, Uid, geteuid},
};

const APP_ID: u32 = 10001;
const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum ClaimKind {
    Session,
    Worktree,
}

#[derive(Debug)]
pub enum ClaimError {
    InvalidId,
    Symlink,
    Busy,
    WrongKind,
    Quota,
    Io(io::Error),
}

impl From<io::Error> for ClaimError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<Errno> for ClaimError {
    fn from(error: Errno) -> Self {
        if error == Errno::LOOP {
            Self::Symlink
        } else {
            Self::Io(error.into())
        }
    }
}

pub struct SessionRoot {
    path: PathBuf,
}

impl SessionRoot {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn try_claim(&self, chat_id: &str, kind: ClaimKind) -> Result<Claim, ClaimError> {
        if chat_id.is_empty()
            || chat_id.len() > 128
            || !chat_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        {
            return Err(ClaimError::InvalidId);
        }
        if fs::symlink_metadata(&self.path).is_ok_and(|metadata| metadata.is_symlink()) {
            return Err(ClaimError::Symlink);
        }
        fs::create_dir_all(&self.path)?;
        let root = unix::open(&self.path, DIR_FLAGS, Mode::empty())?;
        reject_session_symlink(&root, chat_id)?;
        let filename = match kind {
            ClaimKind::Session => format!(".{chat_id}.lock"),
            ClaimKind::Worktree => format!(".worktrees-{chat_id}.lock"),
        };
        let file = unix::openat(
            &root,
            filename,
            OFlags::CREATE | OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
            Mode::from_bits_truncate(0o600),
        )?;
        let stat = unix::fstat(&file)?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile || stat.st_nlink != 1 {
            return Err(io::Error::other("claim lock is not a private regular file").into());
        }
        normalize_fd(&file, stat)?;
        match unix::flock(&file, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => (),
            Err(Errno::WOULDBLOCK) => return Err(ClaimError::Busy),
            Err(error) => return Err(error.into()),
        }
        Ok(Claim {
            _file: file,
            root,
            chat_id: chat_id.into(),
            kind,
        })
    }
}

pub struct Claim {
    _file: OwnedFd,
    root: OwnedFd,
    chat_id: String,
    kind: ClaimKind,
}

pub struct SessionSubpath<'a> {
    pub subpath: &'a str,
    pub target: &'static str,
}

impl Claim {
    /// Call only after Docker confirms no surviving container owns the mount.
    /// Keep this claim through removal so the backend cannot delete state in use.
    pub fn prepare_session(&self, byte_limit: u64) -> Result<SessionSubpath<'_>, ClaimError> {
        if self.kind != ClaimKind::Session {
            return Err(ClaimError::WrongKind);
        }
        match unix::mkdirat(
            &self.root,
            self.chat_id.as_str(),
            Mode::from_bits_truncate(0o700),
        ) {
            Ok(()) | Err(Errno::EXIST) => (),
            Err(error) => return Err(error.into()),
        }
        reject_session_symlink(&self.root, &self.chat_id)?;
        let dir = unix::openat(&self.root, self.chat_id.as_str(), DIR_FLAGS, Mode::empty())?;
        normalize_fd(&dir, unix::fstat(&dir)?)?;
        unix::fchmod(&dir, Mode::from_bits_truncate(0o700))?;
        if byte_limit == 0 {
            return Err(ClaimError::Quota);
        }
        walk(&dir, 0, byte_limit, &mut 0)?;
        Ok(SessionSubpath {
            subpath: &self.chat_id,
            target: "/chat-session",
        })
    }
}

fn reject_session_symlink(root: &OwnedFd, name: &str) -> Result<(), ClaimError> {
    match unix::statat(root, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) if FileType::from_raw_mode(stat.st_mode) == FileType::Symlink => {
            Err(ClaimError::Symlink)
        }
        Ok(_) | Err(Errno::NOENT) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn needs_normalization(stat: &unix::Stat) -> bool {
    geteuid().is_root() && (stat.st_uid != APP_ID || stat.st_gid != APP_ID)
}

fn normalize_fd(fd: &OwnedFd, stat: unix::Stat) -> Result<(), ClaimError> {
    if needs_normalization(&stat) {
        unix::fchown(fd, Some(Uid::from_raw(APP_ID)), Some(Gid::from_raw(APP_ID)))?;
    }
    Ok(())
}

fn walk(dir: &OwnedFd, depth: usize, limit: u64, total: &mut u64) -> Result<(), ClaimError> {
    if depth >= 256 {
        return Err(io::Error::other("session path nesting limit reached").into());
    }
    for entry in Dir::read_from(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        if matches!(name.to_bytes(), b"." | b"..") {
            continue;
        }
        let stat = unix::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW)?;
        let kind = FileType::from_raw_mode(stat.st_mode);
        if kind == FileType::Symlink {
            return Err(ClaimError::Symlink);
        }
        if needs_normalization(&stat) {
            // Do not change the owner of other hard-link aliases outside the session.
            if kind != FileType::Directory && stat.st_nlink != 1 {
                return Err(io::Error::other("session entry has multiple hard links").into());
            }
            unix::chownat(
                dir,
                name,
                Some(Uid::from_raw(APP_ID)),
                Some(Gid::from_raw(APP_ID)),
                AtFlags::SYMLINK_NOFOLLOW,
            )?;
        }
        if kind == FileType::Directory {
            let sub = unix::openat(dir, name, DIR_FLAGS, Mode::empty())?;
            walk(&sub, depth + 1, limit, total)?;
        } else if kind == FileType::RegularFile {
            *total = total.saturating_add(stat.st_size.max(0) as u64);
            if *total >= limit {
                return Err(ClaimError::Quota);
            }
        }
    }
    Ok(())
}
