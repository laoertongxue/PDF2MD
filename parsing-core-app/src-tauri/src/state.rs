use serde::Serialize;

#[derive(Debug, Default)]
pub struct AppState {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub sidecar_child: Option<std::process::Child>,
}

#[derive(Serialize)]
pub struct StatusPayload {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
}
