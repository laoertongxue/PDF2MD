use serde::Serialize;

#[derive(Debug, Default)]
pub struct AppState {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub sidecar_child: Option<tauri_plugin_shell::process::CommandChild>,
}

#[derive(Serialize)]
pub struct StatusPayload {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
}
