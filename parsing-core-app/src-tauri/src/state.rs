use serde::Serialize;

#[derive(Debug, Default)]
pub struct AppState {
    pub port: u16,
    pub health_token: String,
    pub starting: bool,
    pub running: bool,
    pub logs: Vec<String>,
    pub health_failures: u8,
    pub sidecar_child: Option<std::process::Child>,
    pub reserved_listener: Option<std::net::TcpListener>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StatusPayload {
    pub port: u16,
    pub running: bool,
    pub logs: Vec<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiConfig {
    pub api_base: String,
    pub port: u16,
}
