#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod sidecar;
mod state;

use state::{ApiConfig, AppState, StatusPayload};
use std::sync::{Arc, Mutex};
use tauri::Manager;

type SharedState = Arc<Mutex<AppState>>;
const TEXTBOOK_EXTENSIONS: &[&str] = &[
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "png", "jpg", "jpeg", "gif",
    "bmp", "tif", "tiff", "webp",
];

#[tauri::command]
fn get_status(state: tauri::State<SharedState>) -> StatusPayload {
    let s = state.lock().unwrap();
    StatusPayload {
        port: s.port,
        state: s.service_state.clone(),
        error: s.error.clone(),
        log_path: s.log_path.clone(),
        logs: s.logs.clone(),
    }
}

#[tauri::command]
fn get_api_config(state: tauri::State<SharedState>) -> ApiConfig {
    let s = state.lock().unwrap();
    ApiConfig { api_base: format!("http://127.0.0.1:{}", s.port), port: s.port }
}

#[tauri::command]
async fn start_service(
    app: tauri::AppHandle,
    state: tauri::State<'_, SharedState>,
) -> Result<String, String> {
    sidecar::start_sidecar(&app, state.inner().clone()).await?;
    Ok("started".into())
}

#[tauri::command]
async fn stop_service(state: tauri::State<'_, SharedState>) -> Result<String, String> {
    sidecar::stop_sidecar(state.inner().clone())?;
    Ok("stopped".into())
}

#[tauri::command]
async fn retry_service(app: tauri::AppHandle, state: tauri::State<'_, SharedState>) -> Result<String, String> {
    sidecar::restart_sidecar(&app, state.inner().clone()).await?;
    Ok("restarting".into())
}

#[tauri::command]
async fn pick_files(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let files = app.dialog().file().blocking_pick_files();
    match files {
        Some(paths) => Ok(paths.iter().map(|p| p.as_path().unwrap().to_string_lossy().to_string()).collect()),
        None => Ok(vec![]),
    }
}

#[tauri::command]
async fn pick_textbooks(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let files = app
        .dialog()
        .file()
        .add_filter("教材", TEXTBOOK_EXTENSIONS)
        .blocking_pick_files();
    Ok(files
        .unwrap_or_default()
        .iter()
        .filter_map(|path| path.as_path())
        .map(|path| path.to_string_lossy().to_string())
        .collect())
}

#[tauri::command]
fn textbook_path_is_file(path: String) -> bool {
    std::path::Path::new(&path).is_file()
}

#[cfg(test)]
mod textbook_picker_tests {
    use super::TEXTBOOK_EXTENSIONS;

    #[test]
    fn picker_matches_the_supported_textbook_contract() {
        for extension in [
            "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "png", "jpg", "jpeg",
            "webp", "tiff", "bmp",
        ] {
            assert!(TEXTBOOK_EXTENSIONS.contains(&extension));
        }
        for extension in ["csv", "md", "txt"] {
            assert!(!TEXTBOOK_EXTENSIONS.contains(&extension));
        }
    }
}

#[tauri::command]
async fn pick_directory(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let folder = app.dialog().file().blocking_pick_folder();
    Ok(folder.map(|p| p.as_path().unwrap().to_string_lossy().to_string()))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let (port_guard, port) = sidecar::reserve_loopback_port()
                .map_err(|error| format!("failed to reserve sidecar port: {error}"))?;
            let s = SharedState::new(Mutex::new(AppState {
                port,
                health_token: uuid::Uuid::new_v4().to_string(),
                starting: false,
                running: false,
                service_state: "starting".into(),
                error: None,
                log_path: None,
                logs: vec![format!("[init] starting local service on 127.0.0.1:{port}")],
                health_failures: 0,
                sidecar_child: None,
                reserved_listener: Some(port_guard),
            }));
            app.manage(s.clone());
            let app_handle = app.handle().clone();
            let state = s.clone();
            tauri::async_runtime::spawn(async move {
                if let Err(err) = sidecar::start_sidecar(&app_handle, state.clone()).await {
                    let category = sidecar::classify_startup_error(&err);
                    sidecar::record_failure(&state, category, err);
                }
            });
            tauri::async_runtime::spawn(sidecar::health_loop(app.handle().clone(), s));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_api_config,
            start_service,
            stop_service,
            retry_service,
            pick_files,
            pick_textbooks,
            textbook_path_is_file,
            pick_directory
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }) {
                let state = app.state::<SharedState>().inner().clone();
                let _ = sidecar::stop_sidecar(state);
            }
        });
}
