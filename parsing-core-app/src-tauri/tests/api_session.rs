use serde_json::Value;
use std::{collections::BTreeSet, fs, path::Path, path::PathBuf, process::Command};

fn manifest_path(relative: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(relative)
}

fn read(relative: &str) -> String {
    fs::read_to_string(manifest_path(relative))
        .unwrap_or_else(|error| panic!("failed to read {relative}: {error}"))
}

fn read_json(relative: &str) -> Value {
    serde_json::from_str(&read(relative))
        .unwrap_or_else(|error| panic!("failed to parse {relative}: {error}"))
}

fn append_production_frontend_source(directory: &Path, source: &mut String) {
    for entry in fs::read_dir(directory)
        .unwrap_or_else(|error| panic!("failed to read {}: {error}", directory.display()))
    {
        let entry = entry.expect("frontend source directory entry");
        let path = entry.path();
        if path.is_dir() {
            append_production_frontend_source(&path, source);
            continue;
        }
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        let is_source = matches!(
            path.extension().and_then(|value| value.to_str()),
            Some("ts" | "tsx")
        );
        if !is_source || name.contains(".test.") || name.contains(".spec.") {
            continue;
        }
        source.push_str(
            &fs::read_to_string(&path)
                .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display())),
        );
        source.push('\n');
    }
}

#[test]
fn main_capability_exactly_matches_production_webview_call_facts() {
    let capability = read_json("capabilities/main.json");
    let permissions = capability["permissions"]
        .as_array()
        .expect("main capability permissions must be an array");
    let actual = permissions
        .iter()
        .map(|permission| {
            permission
                .as_str()
                .expect("main capability permissions must be strings")
        })
        .collect::<BTreeSet<_>>();
    let expected = BTreeSet::from(["core:event:allow-listen", "core:event:allow-unlisten"]);

    assert_eq!(
        actual, expected,
        "the main WebView only needs event listen/unlisten for onDragDropEvent"
    );

    let mut frontend = String::new();
    append_production_frontend_source(&manifest_path("../src"), &mut frontend);
    assert!(frontend.contains("@tauri-apps/api/webview"));
    assert!(frontend.contains(".onDragDropEvent("));
    for forbidden_call_surface in [
        "@tauri-apps/api/event",
        "@tauri-apps/api/window",
        "@tauri-apps/api/tray",
        "@tauri-apps/plugin-dialog",
        ".emit(",
    ] {
        assert!(
            !frontend.contains(forbidden_call_surface),
            "unexpected privileged frontend call surface: {forbidden_call_surface}"
        );
    }
}

#[test]
fn tauri_configuration_has_no_shell_plugin() {
    let config = read_json("tauri.conf.json");
    assert!(
        config
            .get("plugins")
            .and_then(|plugins| plugins.get("shell"))
            .is_none(),
        "tauri.conf.json must not configure the shell plugin"
    );
}

#[test]
fn cargo_uses_single_instance_without_shell() {
    let manifest = read("Cargo.toml");
    assert!(
        manifest.lines().any(|line| {
            let line = line.trim();
            line.starts_with("tauri-plugin-single-instance") && line.ends_with("= \"2\"")
        }),
        "Cargo.toml must depend on tauri-plugin-single-instance v2"
    );
    assert!(
        !manifest.contains("tauri-plugin-shell"),
        "Cargo.toml must not depend on tauri-plugin-shell"
    );
}

#[test]
fn single_instance_is_first_plugin_and_restores_the_main_window() {
    let source = read("src/main.rs");
    let first_plugin = source
        .find(".plugin(")
        .expect("the Tauri builder must register plugins");
    let single_instance = source
        .find(".plugin(tauri_plugin_single_instance::init(")
        .expect("single-instance plugin must be registered");
    let dialog = source
        .find(".plugin(tauri_plugin_dialog::init())")
        .expect("dialog plugin must remain registered");

    assert_eq!(
        first_plugin, single_instance,
        "single-instance must be the first registered plugin"
    );
    assert!(single_instance < dialog);
    for operation in [
        "get_webview_window(\"main\")",
        "window.show()",
        "window.unminimize()",
        "window.set_focus()",
    ] {
        assert!(
            source.contains(operation),
            "single-instance callback must contain {operation}"
        );
    }
    assert!(
        !source.contains("tauri_plugin_shell"),
        "the application must not initialize the shell plugin"
    );
}

#[test]
fn packaged_macos_exit_paths_share_controlled_cleanup_and_preserve_exit_codes() {
    let source = read("src/main.rs");
    for event_contract in [
        "tauri::RunEvent::ExitRequested { code, api, .. }",
        "handle_application_exit_request(app, &state, code, &api)",
        "event: tauri::WindowEvent::CloseRequested { api, .. }",
        "handle_window_close_request(app, &state, &api)",
        "begin_exit_request(state, code)",
        "begin_exit_request(state, None)",
        "api.prevent_exit()",
        "api.prevent_close()",
        "app.run_return(",
        "std::process::exit(exit_code)",
    ] {
        assert!(
            source.contains(event_contract),
            "missing packaged exit contract: {event_contract}"
        );
    }
    assert_eq!(
        source
            .matches("launch_controlled_exit(app.clone(), state.clone())")
            .count(),
        2,
        "window close and application quit must use the same cleanup launcher"
    );

    let config = read_json("tauri.conf.json");
    let external_bins = config["bundle"]["externalBin"]
        .as_array()
        .expect("bundle.externalBin must be an array");
    assert!(
        external_bins
            .iter()
            .any(|value| value.as_str() == Some("binaries/python3")),
        "the parent-monitored wrapper must be the packaged sidecar entrypoint"
    );

    let runtime_helper = manifest_path("../scripts/sidecar_runtime.py");
    let launcher = Command::new("/usr/bin/python3")
        .args(["-I", "-S", "-B"])
        .arg(&runtime_helper)
        .arg("emit-launcher")
        .env_clear()
        .env("HOME", "/tmp")
        .env("PATH", "/usr/bin:/bin")
        .env("TMPDIR", "/tmp")
        .output()
        .unwrap_or_else(|error| {
            panic!(
                "failed to execute production launcher emitter {}: {error}",
                runtime_helper.display()
            )
        });
    assert!(
        launcher.status.success(),
        "production launcher emitter failed: {}",
        String::from_utf8_lossy(&launcher.stderr)
    );
    assert!(launcher.stderr.is_empty());
    let wrapper = String::from_utf8(launcher.stdout).expect("launcher must be UTF-8");
    for launcher_contract in [
        "--parent-pid",
        "--socket-fd",
        "--session-token-fd",
        "exec \"$python\" -s -B -m parsing_core.serving.lifecycle",
    ] {
        assert!(
            wrapper.contains(launcher_contract),
            "missing packaged sidecar launcher contract: {launcher_contract}"
        );
    }
    assert!(!wrapper.contains("/bin/ps"));
    assert!(!wrapper.contains("PDF2MD_SESSION_TOKEN"));

    let lifecycle = read("../../src/parsing_core/serving/lifecycle.py");
    for watchdog_contract in [
        "secrets.token_hex(OWNER_MARKER_BYTES)",
        "select.KQ_NOTE_EXIT",
        "KERN_PROCARGS2",
        "pbi_start_tvusec",
        "os.getuid()",
        "stable_empty_scans >= 2",
        "cleanup_marked_processes",
    ] {
        assert!(
            lifecycle.contains(watchdog_contract),
            "missing marker watchdog contract: {watchdog_contract}"
        );
    }
}
