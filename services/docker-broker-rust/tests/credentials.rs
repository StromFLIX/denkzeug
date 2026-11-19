use std::{collections::BTreeSet, io::Read};

use denkzeug_docker_broker_spike::credentials::{Batch, Delivery, activation};
use serde_json::{Value, json};

fn fixture() -> Value {
    json!([{"full_name":"owner/repo","token":"synthetic.private+/=~","expires_at":"2099-01-01T00:00:00Z"}])
}

#[test]
fn invalid_batches_fail_without_echoing_input_and_scope_cannot_change() {
    let expected = BTreeSet::from(["owner/repo".to_owned()]);
    let value = fixture();
    for (field, invalid) in [
        ("token", ""),
        ("token", "private\nusername=other"),
        ("token", "private\u{7f}"),
        ("expires_at", "private-invalid"),
        ("expires_at", "2099-01-01T00:00:00"),
        ("full_name", "../owner/repo"),
    ] {
        let mut bad = value.clone();
        bad[0][field] = json!(invalid);
        let message = Batch::validate(bad, Some(&expected))
            .err()
            .unwrap()
            .to_string();
        assert!(!message.contains("private"));
    }
    for bad in [
        Value::Null,
        json!([]),
        json!([value[0], value[0]]),
        json!(vec![value[0].clone(); 21]),
    ] {
        assert!(Batch::validate(bad, None).is_err());
    }
    for field in ["full_name", "token", "expires_at"] {
        let mut bad = value.clone();
        bad[0].as_object_mut().unwrap().remove(field);
        assert!(Batch::validate(bad, None).is_err());
        let mut bad = value.clone();
        bad[0][field] = json!(123);
        assert!(Batch::validate(bad, None).is_err());
    }
    assert!(
        Batch::validate(
            value.clone(),
            Some(&BTreeSet::from(["owner/elsewhere".into()]))
        )
        .is_err()
    );
    assert!(Batch::validate(value, Some(&expected)).is_ok());
}

#[test]
fn initial_and_renewed_delivery_use_fixed_paths_owner_and_modes() {
    let data = fixture();
    let batch = Batch::validate(data.clone(), None).unwrap();
    for (delivery, expected_destination, expected_file) in [
        (
            Delivery::Initial,
            "/tmp",
            "nautionette-git-credentials/current.json",
        ),
        (
            Delivery::Renewal,
            "/tmp/nautionette-git-credentials",
            "pending.json",
        ),
    ] {
        let (destination, archive) = batch.archive(delivery).unwrap();
        assert_eq!(destination, expected_destination);
        let mut reader = tar::Archive::new(archive.as_slice());
        let mut entries = reader.entries().unwrap();
        if delivery == Delivery::Initial {
            let directory = entries.next().unwrap().unwrap();
            assert!(directory.header().entry_type().is_dir());
            assert_eq!(directory.header().mode().unwrap(), 0o711);
            assert_eq!(directory.header().uid().unwrap(), 0);
        }
        let mut file = entries.next().unwrap().unwrap();
        assert_eq!(file.path().unwrap().to_str(), Some(expected_file));
        assert_eq!(
            (
                file.header().uid().unwrap(),
                file.header().gid().unwrap(),
                file.header().mode().unwrap()
            ),
            (0, 10001, 0o440)
        );
        let mut raw = Vec::new();
        file.read_to_end(&mut raw).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&raw).unwrap(), data);
        assert!(entries.next().is_none());
    }
    let (user, command) = activation();
    assert_eq!(user, "0:0");
    assert_eq!(command[0..2], ["node", "-e"]);
    assert!(
        command[2].contains("renameSync")
            && command[2].contains("pending.json")
            && command[2].contains("current.json")
    );
    assert!(!command.concat().contains("synthetic"));
}
