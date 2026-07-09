import platform
import subprocess


class KeychainError(RuntimeError):
    pass


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain is required")


def save_secret(service: str, account: str, secret: str) -> None:
    _require_macos()
    cmd = [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-w",
        secret,
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise KeychainError(result.stderr.strip() or "failed to save secret")


def read_secret(service: str, account: str) -> str:
    _require_macos()
    cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise KeychainError(result.stderr.strip() or "secret not found")
    return result.stdout.strip()


def delete_secret(service: str, account: str) -> None:
    _require_macos()
    cmd = ["security", "delete-generic-password", "-s", service, "-a", account]
    subprocess.run(cmd, text=True, capture_output=True, check=False)


def mask_secret(secret: str) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "****"
    return f"{secret[:3]}****{secret[-4:]}"
