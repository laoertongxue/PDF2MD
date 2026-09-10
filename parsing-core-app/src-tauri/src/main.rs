#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod sidecar;
mod sidecar_log;
mod state;

use state::{lock_state, ready_api_config, ApiConfig, AppState, StatusPayload};
use std::sync::{Arc, Mutex};
use tauri::Manager;

type SharedState = Arc<Mutex<AppState>>;
const TEXTBOOK_EXTENSIONS: &[&str] = &[
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "png", "jpg", "jpeg", "gif", "bmp", "tif",
    "tiff", "webp",
];
const FORCED_EXIT_CODE: i32 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExitRequestAction {
    Allow,
    Prevent,
    PreventAndStartCleanup,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ForceExitOutcome {
    CleanExitRequested,
    Cancelled,
    ForcedExitRequested,
}

fn begin_exit_request(state: &SharedState, requested_code: Option<i32>) -> ExitRequestAction {
    let mut state = lock_state(state);
    if state.approved_exit_code.is_some_and(|approved_code| {
        requested_code.is_none() || requested_code == Some(approved_code)
    }) {
        return ExitRequestAction::Allow;
    }
    if state.exit_cleanup_in_flight {
        return ExitRequestAction::Prevent;
    }
    state.exit_cleanup_in_flight = true;
    ExitRequestAction::PreventAndStartCleanup
}

fn complete_controlled_exit<E>(
    state: &SharedState,
    cleanup: Result<(), String>,
    exit: E,
) -> Result<(), String>
where
    E: FnOnce(i32),
{
    match cleanup {
        Ok(()) => {
            {
                let mut state = lock_state(state);
                state.exit_cleanup_in_flight = false;
                state.approved_exit_code = Some(0);
            }
            exit(0);
            Ok(())
        }
        Err(error) => {
            lock_state(state).exit_cleanup_in_flight = false;
            Err(error)
        }
    }
}

struct ExitCleanupSlot {
    state: SharedState,
}

impl Drop for ExitCleanupSlot {
    fn drop(&mut self) {
        lock_state(&self.state).exit_cleanup_in_flight = false;
    }
}

fn begin_failed_cleanup_retry(state: &SharedState) -> Result<ExitCleanupSlot, String> {
    let shared_state = Arc::clone(state);
    let mut state = lock_state(state);
    if !state.force_exit_available() {
        return Err("force exit is unavailable without a shutdown cleanup failure".into());
    }
    if state.exit_cleanup_in_flight {
        return Err("exit cleanup is already in progress".into());
    }
    state.exit_cleanup_in_flight = true;
    Ok(ExitCleanupSlot {
        state: shared_state,
    })
}

fn force_exit_after_cleanup_failure<C, D, E>(
    state: &SharedState,
    cleanup: C,
    confirm: D,
    exit: E,
) -> Result<ForceExitOutcome, String>
where
    C: FnOnce() -> Result<(), String>,
    D: FnOnce() -> bool,
    E: FnOnce(i32),
{
    let _cleanup_slot = begin_failed_cleanup_retry(state)?;
    let cleanup = cleanup();
    if cleanup.is_ok() {
        lock_state(state).approved_exit_code = Some(0);
        exit(0);
        return Ok(ForceExitOutcome::CleanExitRequested);
    }
    if !lock_state(state).force_exit_available() {
        return Err("force exit is unavailable without a shutdown cleanup failure".into());
    }
    if !confirm() {
        return Ok(ForceExitOutcome::Cancelled);
    }
    lock_state(state).approved_exit_code = Some(FORCED_EXIT_CODE);
    exit(FORCED_EXIT_CODE);
    Ok(ForceExitOutcome::ForcedExitRequested)
}

fn restore_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn launch_controlled_exit(app: tauri::AppHandle, state: SharedState) {
    tauri::async_runtime::spawn(async move {
        let cleanup_state = state.clone();
        let cleanup = tauri::async_runtime::spawn_blocking(move || {
            sidecar::shutdown_sidecar_for_exit(cleanup_state)
        })
        .await
        .map_err(|error| format!("exit cleanup task failed: {error}"))
        .and_then(|result| result);
        let exit_app = app.clone();
        if let Err(error) = complete_controlled_exit(&state, cleanup, move |code| {
            exit_app.exit(code);
        }) {
            restore_main_window(&app);
            eprintln!("sidecar exit cleanup failed: {error}");
        }
    });
}

fn handle_application_exit_request(
    app: &tauri::AppHandle,
    state: &SharedState,
    code: Option<i32>,
    api: &tauri::ExitRequestApi,
) {
    match begin_exit_request(state, code) {
        ExitRequestAction::Allow => {}
        ExitRequestAction::Prevent => api.prevent_exit(),
        ExitRequestAction::PreventAndStartCleanup => {
            api.prevent_exit();
            launch_controlled_exit(app.clone(), state.clone());
        }
    }
}

fn handle_window_close_request(
    app: &tauri::AppHandle,
    state: &SharedState,
    api: &tauri::CloseRequestApi,
) {
    match begin_exit_request(state, None) {
        ExitRequestAction::Allow => {}
        ExitRequestAction::Prevent => api.prevent_close(),
        ExitRequestAction::PreventAndStartCleanup => {
            api.prevent_close();
            launch_controlled_exit(app.clone(), state.clone());
        }
    }
}

#[tauri::command]
fn get_status(state: tauri::State<SharedState>) -> StatusPayload {
    let s = lock_state(state.inner());
    StatusPayload {
        port: s.port,
        state: s.service_state.clone(),
        error: s.error.clone(),
        log_path: s.log_path.clone(),
        logs: s.logs.clone(),
        desired_running: s.desired_running,
        manual_stopped: s.manual_stopped,
        force_exit_available: s.force_exit_available(),
    }
}

#[tauri::command]
fn get_api_config(state: tauri::State<SharedState>) -> Result<ApiConfig, String> {
    let s = lock_state(state.inner());
    ready_api_config(&s)
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
    sidecar::stop_sidecar_async(state.inner().clone(), sidecar::StopReason::Manual).await?;
    Ok("stopped".into())
}

#[tauri::command]
async fn retry_service(
    app: tauri::AppHandle,
    state: tauri::State<'_, SharedState>,
) -> Result<String, String> {
    sidecar::retry_sidecar(&app, state.inner().clone()).await?;
    Ok("restarting".into())
}

#[tauri::command]
async fn retry_exit_cleanup(
    app: tauri::AppHandle,
    state: tauri::State<'_, SharedState>,
) -> Result<String, String> {
    let state = state.inner().clone();
    let cleanup_slot = begin_failed_cleanup_retry(&state)
        .map_err(|error| format!("exit cleanup retry is unavailable: {error}"))?;
    let cleanup_state = state.clone();
    let (cleanup, _cleanup_slot) = tauri::async_runtime::spawn_blocking(move || {
        (
            sidecar::shutdown_sidecar_for_exit(cleanup_state),
            cleanup_slot,
        )
    })
    .await
    .map_err(|error| format!("exit cleanup retry task failed: {error}"))?;
    let exit_app = app.clone();
    complete_controlled_exit(&state, cleanup, move |code| exit_app.exit(code))?;
    Ok("exit_requested".into())
}

#[tauri::command]
async fn request_force_exit_confirmation(
    app: tauri::AppHandle,
    state: tauri::State<'_, SharedState>,
) -> Result<String, String> {
    let state = state.inner().clone();
    let cleanup_state = state.clone();
    let dialog_app = app.clone();
    let exit_app = app.clone();
    let outcome = tauri::async_runtime::spawn_blocking(move || {
        force_exit_after_cleanup_failure(
            &state,
            move || sidecar::shutdown_sidecar_for_exit(cleanup_state),
            move || {
                use tauri_plugin_dialog::{
                    DialogExt, MessageDialogButtons, MessageDialogKind,
                };
                dialog_app
                    .dialog()
                    .message(
                        "本地服务仍可能有未清理的进程。强制退出可能丢失当前任务状态。\n\nThe local service may still contain unmanaged processes. Force quitting may lose current task state.",
                    )
                    .title("PDF2MD")
                    .kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        "强制退出 / Force Quit".into(),
                        "取消 / Cancel".into(),
                    ))
                    .blocking_show()
            },
            move |code| exit_app.exit(code),
        )
    })
    .await
    .map_err(|error| format!("force exit confirmation task failed: {error}"))??;
    Ok(match outcome {
        ForceExitOutcome::CleanExitRequested => "exit_requested",
        ForceExitOutcome::Cancelled => "force_exit_cancelled",
        ForceExitOutcome::ForcedExitRequested => "force_exit_requested",
    }
    .into())
}

#[tauri::command]
async fn pick_files(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let files = app.dialog().file().blocking_pick_files();
    match files {
        Some(paths) => Ok(paths
            .iter()
            .map(|p| p.as_path().unwrap().to_string_lossy().to_string())
            .collect()),
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
            "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "png", "jpg", "jpeg", "webp",
            "tiff", "bmp",
        ] {
            assert!(TEXTBOOK_EXTENSIONS.contains(&extension));
        }
        for extension in ["csv", "md", "txt"] {
            assert!(!TEXTBOOK_EXTENSIONS.contains(&extension));
        }
    }
}

#[cfg(test)]
mod exit_event_tests {
    use crate::state::{AppState, ServiceError};
    use std::cell::Cell;
    use std::sync::{Arc, Mutex};

    #[test]
    fn confirmed_force_exit_code_bypasses_cleanup_and_prevention() {
        let state = Arc::new(Mutex::new(AppState {
            approved_exit_code: Some(super::FORCED_EXIT_CODE),
            ..Default::default()
        }));

        assert_eq!(
            super::begin_exit_request(&state, Some(super::FORCED_EXIT_CODE)),
            super::ExitRequestAction::Allow
        );
    }

    #[test]
    fn unapproved_programmatic_exit_enters_the_controlled_cleanup_path() {
        let state = Arc::new(Mutex::new(AppState::default()));

        assert_eq!(
            super::begin_exit_request(&state, Some(super::FORCED_EXIT_CODE)),
            super::ExitRequestAction::PreventAndStartCleanup
        );
    }

    #[test]
    fn cmd_q_and_dock_quit_share_a_single_controlled_cleanup() {
        let state = Arc::new(Mutex::new(AppState::default()));

        assert_eq!(
            super::begin_exit_request(&state, None),
            super::ExitRequestAction::PreventAndStartCleanup
        );
        assert_eq!(
            super::begin_exit_request(&state, None),
            super::ExitRequestAction::Prevent
        );
    }

    #[test]
    fn controlled_cleanup_success_approves_and_requests_normal_exit() {
        let state = Arc::new(Mutex::new(AppState {
            exit_cleanup_in_flight: true,
            ..Default::default()
        }));
        let exit_code = Cell::new(None);

        let result = super::complete_controlled_exit(&state, Ok(()), |code| {
            exit_code.set(Some(code));
        });

        assert_eq!(result, Ok(()));
        assert_eq!(exit_code.get(), Some(0));
        let state = state.lock().unwrap();
        assert_eq!(state.approved_exit_code, Some(0));
        assert!(!state.exit_cleanup_in_flight);
    }

    #[test]
    fn controlled_cleanup_failure_releases_single_flight_without_approving_exit() {
        let state = Arc::new(Mutex::new(AppState {
            exit_cleanup_in_flight: true,
            ..Default::default()
        }));
        let exit_code = Cell::new(None);

        let result = super::complete_controlled_exit(
            &state,
            Err("stable shutdown failure".into()),
            |code| exit_code.set(Some(code)),
        );

        assert_eq!(result, Err("stable shutdown failure".into()));
        assert_eq!(exit_code.get(), None);
        let state = state.lock().unwrap();
        assert_eq!(state.approved_exit_code, None);
        assert!(!state.exit_cleanup_in_flight);
    }

    #[test]
    fn force_exit_rejects_non_shutdown_state_without_running_callbacks() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "startup".into(),
                message: "startup failed".into(),
            }),
            ..Default::default()
        }));
        let cleanup_called = Cell::new(false);
        let confirm_called = Cell::new(false);
        let exit_code = Cell::new(None);

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || {
                cleanup_called.set(true);
                Ok(())
            },
            || {
                confirm_called.set(true);
                true
            },
            |code| exit_code.set(Some(code)),
        );

        assert!(matches!(
            result,
            Err(ref error) if error == "force exit is unavailable without a shutdown cleanup failure"
        ));
        assert!(!cleanup_called.get());
        assert!(!confirm_called.get());
        assert_eq!(exit_code.get(), None);
    }

    #[test]
    fn force_exit_cancellation_keeps_the_application_running() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup still failed".into(),
            }),
            ..Default::default()
        }));
        let sequence = Cell::new(0);
        let exit_code = Cell::new(None);

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || {
                assert_eq!(sequence.get(), 0);
                sequence.set(1);
                Err("best-effort cleanup still failed".into())
            },
            || {
                assert_eq!(sequence.get(), 1);
                sequence.set(2);
                false
            },
            |code| exit_code.set(Some(code)),
        );

        assert_eq!(result, Ok(super::ForceExitOutcome::Cancelled));
        assert_eq!(sequence.get(), 2);
        assert_eq!(exit_code.get(), None);
    }

    #[test]
    fn force_exit_flow_holds_the_shared_cleanup_slot_until_cancelled() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup still failed".into(),
            }),
            ..Default::default()
        }));

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || {
                assert!(state.lock().unwrap().exit_cleanup_in_flight);
                Err("best-effort cleanup still failed".into())
            },
            || {
                assert!(state.lock().unwrap().exit_cleanup_in_flight);
                false
            },
            |_| panic!("cancelled force exit must not request process exit"),
        );

        assert_eq!(result, Ok(super::ForceExitOutcome::Cancelled));
        assert!(!state.lock().unwrap().exit_cleanup_in_flight);
    }

    #[test]
    fn concurrent_force_exit_request_is_rejected_without_running_callbacks() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            exit_cleanup_in_flight: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup still failed".into(),
            }),
            ..Default::default()
        }));
        let cleanup_called = Cell::new(false);

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || {
                cleanup_called.set(true);
                Err("must not run".into())
            },
            || false,
            |_| panic!("concurrent force exit must not request process exit"),
        );

        assert_eq!(result, Err("exit cleanup is already in progress".into()));
        assert!(!cleanup_called.get());
    }

    #[test]
    fn panicking_force_cleanup_releases_the_shared_cleanup_slot() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup still failed".into(),
            }),
            ..Default::default()
        }));

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = super::force_exit_after_cleanup_failure(
                &state,
                || panic!("simulated cleanup task panic"),
                || false,
                |_| {},
            );
        }));

        assert!(result.is_err());
        assert!(!state.lock().unwrap().exit_cleanup_in_flight);
    }

    #[test]
    fn successful_force_cleanup_requests_normal_exit_without_confirmation() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup previously failed".into(),
            }),
            ..Default::default()
        }));
        let confirm_called = Cell::new(false);
        let exit_code = Cell::new(None);

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || Ok(()),
            || {
                confirm_called.set(true);
                true
            },
            |code| exit_code.set(Some(code)),
        );

        assert_eq!(result, Ok(super::ForceExitOutcome::CleanExitRequested));
        assert!(!confirm_called.get());
        assert_eq!(exit_code.get(), Some(0));
        assert_eq!(state.lock().unwrap().approved_exit_code, Some(0));
    }

    #[test]
    fn confirmed_force_exit_approves_and_uses_the_nonzero_exit_code() {
        let state = Arc::new(Mutex::new(AppState {
            application_exiting: true,
            error: Some(ServiceError {
                category: "shutdown".into(),
                message: "cleanup still failed".into(),
            }),
            ..Default::default()
        }));
        let sequence = Cell::new(0);
        let exit_code = Cell::new(None);

        let result = super::force_exit_after_cleanup_failure(
            &state,
            || {
                assert_eq!(sequence.get(), 0);
                sequence.set(1);
                Err("best-effort cleanup still failed".into())
            },
            || {
                assert_eq!(sequence.get(), 1);
                sequence.set(2);
                true
            },
            |code| {
                assert_eq!(sequence.get(), 2);
                sequence.set(3);
                exit_code.set(Some(code));
            },
        );

        assert_eq!(result, Ok(super::ForceExitOutcome::ForcedExitRequested));
        assert_eq!(sequence.get(), 3);
        assert_eq!(exit_code.get(), Some(super::FORCED_EXIT_CODE));
        assert_eq!(
            state.lock().unwrap().approved_exit_code,
            Some(super::FORCED_EXIT_CODE)
        );
    }
}

#[tauri::command]
async fn pick_directory(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let folder = app.dialog().file().blocking_pick_folder();
    Ok(folder.map(|p| p.as_path().unwrap().to_string_lossy().to_string()))
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let (port_guard, port) = sidecar::reserve_loopback_port()
                .map_err(|error| format!("failed to reserve sidecar port: {error}"))?;
            let s = SharedState::new(Mutex::new(AppState {
                port,
                session_token: String::new(),
                starting: false,
                stopping: false,
                exit_stop_requested: false,
                application_exiting: false,
                exit_cleanup_in_flight: false,
                approved_exit_code: None,
                running: false,
                desired_running: true,
                manual_stopped: false,
                service_state: "starting".into(),
                error: None,
                log_path: None,
                logs: vec![format!("[init] starting local service on 127.0.0.1:{port}")],
                health_failures: 0,
                generation: 0,
                sidecar_owner_generation: None,
                sidecar_child: None,
                sidecar_session_id: None,
                sidecar_process_group: None,
                stopping_session_id: None,
                stopping_process_group: None,
                stop_completed: Arc::new(std::sync::Condvar::new()),
                sidecar_log_threads: Vec::new(),
                logging_cleanup_scheduled: false,
                reserved_listener: Some(port_guard),
            }));
            app.manage(s.clone());
            let app_handle = app.handle().clone();
            let state = s.clone();
            tauri::async_runtime::spawn(async move {
                let _ = sidecar::start_sidecar(&app_handle, state).await;
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
            retry_exit_cleanup,
            request_force_exit_confirmation,
            pick_files,
            pick_textbooks,
            textbook_path_is_file,
            pick_directory
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");
    let exit_code = app.run_return(|app, event| match event {
        tauri::RunEvent::ExitRequested { code, api, .. } => {
            let state = app.state::<SharedState>().inner().clone();
            handle_application_exit_request(app, &state, code, &api);
        }
        tauri::RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::CloseRequested { api, .. },
            ..
        } if label == "main" => {
            let state = app.state::<SharedState>().inner().clone();
            handle_window_close_request(app, &state, &api);
        }
        tauri::RunEvent::Exit => {
            let state = app.state::<SharedState>().inner().clone();
            if lock_state(&state).approved_exit_code.is_none() {
                if let Err(error) = sidecar::shutdown_sidecar_for_exit(state) {
                    eprintln!("final sidecar exit cleanup failed: {error}");
                }
            }
        }
        _ => {}
    });
    std::process::exit(exit_code);
}
