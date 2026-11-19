use std::{
    fs,
    os::unix::{
        fs::{MetadataExt, symlink},
        io::AsFd,
    },
    path::PathBuf,
};

use denkzeug_docker_broker_spike::session::{ClaimError, ClaimKind, SessionRoot};
use rustix::fs::{FlockOperation, flock};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "dz-session-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn root(&self) -> SessionRoot {
        SessionRoot::new(self.0.clone())
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn claims_use_the_same_stable_flock_inode_as_the_python_broker() {
    let fixture = Fixture::new();
    let root = fixture.root();
    let claim = root.try_claim("Chat_ok-1", ClaimKind::Session).unwrap();
    let lock = fixture.0.join(".Chat_ok-1.lock");
    let inode = fs::metadata(&lock).unwrap().ino();
    let external = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock)
        .unwrap();
    assert_eq!(
        flock(external.as_fd(), FlockOperation::NonBlockingLockExclusive),
        Err(rustix::io::Errno::WOULDBLOCK)
    );
    assert!(matches!(
        root.try_claim("Chat_ok-1", ClaimKind::Session),
        Err(ClaimError::Busy)
    ));
    let worktree = root.try_claim("Chat_ok-1", ClaimKind::Worktree).unwrap();
    assert!(fixture.0.join(".worktrees-Chat_ok-1.lock").is_file());
    assert!(matches!(
        worktree.prepare_session(1),
        Err(ClaimError::WrongKind)
    ));
    let prepared = claim.prepare_session(1024).unwrap();
    assert_eq!(prepared.subpath, "Chat_ok-1");
    assert_eq!(prepared.target, "/chat-session");
    assert_eq!(
        fs::metadata(fixture.0.join("Chat_ok-1")).unwrap().mode() & 0o777,
        0o700
    );
    fs::remove_dir(fixture.0.join("Chat_ok-1")).unwrap();
    drop(claim);
    flock(external, FlockOperation::NonBlockingLockExclusive).unwrap();
    let _next = root.try_claim("Chat_ok-1", ClaimKind::Session).unwrap();
    assert_eq!(fs::metadata(lock).unwrap().ino(), inode);
}

#[test]
fn rejects_ambiguous_names_and_symlinks_in_session_mounts_and_locks() {
    let fixture = Fixture::new();
    let root = fixture.root();
    for id in ["", ".", "../private", "sub/directory", "chat.session", "🙃"] {
        assert!(matches!(
            root.try_claim(id, ClaimKind::Session),
            Err(ClaimError::InvalidId)
        ));
    }
    let outside = fixture.0.join("outside");
    fs::create_dir(&outside).unwrap();
    symlink(&outside, fixture.0.join("chat")).unwrap();
    assert!(matches!(
        root.try_claim("chat", ClaimKind::Session),
        Err(ClaimError::Symlink)
    ));
    fs::remove_file(fixture.0.join("chat")).unwrap();
    let lock_outside = outside.join("lock");
    fs::write(&lock_outside, b"keep me").unwrap();
    symlink(&lock_outside, fixture.0.join(".chat.lock")).unwrap();
    assert!(matches!(
        root.try_claim("chat", ClaimKind::Session),
        Err(ClaimError::Symlink)
    ));
    assert_eq!(fs::read(lock_outside).unwrap(), b"keep me");
}

#[test]
fn durable_session_tree_rejects_symlinks_and_checks_nested_bytes_without_removing_state() {
    let fixture = Fixture::new();
    let claim = fixture
        .root()
        .try_claim("chat", ClaimKind::Session)
        .unwrap();
    let path = fixture.0.join("chat");
    fs::create_dir_all(path.join("nested")).unwrap();
    fs::write(path.join("nested/state"), b"prior durable state").unwrap();
    assert!(matches!(claim.prepare_session(19), Err(ClaimError::Quota)));
    assert!(claim.prepare_session(20).is_ok());
    let shortcut = path.join("nested/link");
    symlink(&path, shortcut).unwrap();
    assert!(matches!(
        claim.prepare_session(4096),
        Err(ClaimError::Symlink)
    ));
    assert_eq!(
        fs::read(path.join("nested/state")).unwrap(),
        b"prior durable state"
    );
}
