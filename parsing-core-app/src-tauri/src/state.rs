use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceError {
    pub category: String,
    pub message: String,
}

#[derive(Debug, Default)]
pub struct AppState {
    pub port: u16,
    pub health_token: String,
    pub starting: bool,
    pub running: bool,
    pub desired_running: bool,
    pub manual_stopped: bool,
    pub service_state: String,
    pub error: Option<ServiceError>,
    pub log_path: Option<String>,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub sidecar_child: Option<std::process::Child>,
    pub reserved_listener: Option<std::net::TcpListener>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StatusPayload {
    pub port: u16,
    pub state: String,
    pub error: Option<ServiceError>,
    pub log_path: Option<String>,
    pub logs: Vec<String>,
    pub desired_running: bool,
    pub manual_stopped: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiConfig {
    pub api_base: String,
    pub port: u16,
}
