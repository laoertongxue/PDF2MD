#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod sidecar;
mod state;

use state::{AppState, StatusPayload};
use std::sync::{Arc, Mutex};
use tauri::Manager;

type SharedState = Arc<Mutex<AppState>>;

#[tauri::command]
fn get_status(state: tauri::State<'_, SharedState>) -> StatusPayload {
    let s = state.lock().unwrap();
    StatusPayload {
        port: s.port,
        running: s.running,
        logs: s.logs.clone(),
    }
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
    sidecar::stop_sidecar(state.inner().clone()).await?;
    Ok("stopped".into())
}

#[tauri::command]
async fn pick_files(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let files = app
        .dialog()
        .file()
        .blocking_pick_files();
    match files {
        Some(paths) => Ok(paths.iter().map(|p| p.as_path().unwrap().to_string_lossy().to_string()).collect()),
        None => Ok(vec![]),
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let tray = tauri::tray::TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(
                    &tauri::menu::MenuBuilder::new(app)
                        .item(&tauri::menu::MenuItemBuilder::with_id("show", "显示").build(app)?)
                        .item(&tauri::menu::MenuItemBuilder::with_id("quit", "退出").build(app)?)
                        .build()?,
                )
                .on_menu_event(|app, event| {
                    match event.id().as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "quit" => {
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .build(app)?;
            tray.set_tooltip(Some("parsing-core"))?;

            let port = sidecar::find_free_port(8000);
            let s = SharedState::new(Mutex::new(AppState {
                port,
                running: false,
                logs: vec![format!("[init] port {} selected", port)],
                health_failures: 0,
                sidecar_child: None,
            }));
            app.manage(s.clone());

            let app_handle = app.handle().clone();
            let state_clone = s.clone();
            std::thread::spawn(move || {
                let rt = tokio::runtime::Runtime::new().unwrap();
                rt.block_on(async { let _ = sidecar::start_sidecar(&app_handle, state_clone).await; });
            });

            let app_h2 = app.handle().clone();
            let state_h = s.clone();
            std::thread::spawn(move || {
                let rt = tokio::runtime::Runtime::new().unwrap();
                rt.block_on(async { sidecar::health_loop(app_h2, state_h).await; });
            });

            Ok(())
        })
        .on_window_event(|w, e| {
            if let tauri::WindowEvent::CloseRequested { .. } = e {
                let _ = w.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![get_status, start_service, stop_service, pick_files])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
