# macOS 代码签名与公证指南

## 前置条件

- Apple Developer 账号（$99/年）
- Xcode 已安装
- 在 Keychain 中已导入 Developer ID Application 证书

## 步骤

### 1. 创建 entitlements 文件

```bash
cat > parsing-core-app/src-tauri/entitlements.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.network.client</key>
    <true/>
</dict>
</plist>
EOF
```

### 2. 配置 tauri.conf.json

```json
{
  "bundle": {
    "macOS": {
      "signingIdentity": "Developer ID Application: Your Name (TEAMID)",
      "entitlements": "entitlements.plist"
    }
  }
}
```

### 3. 签名并公证

```bash
cd parsing-core-app

# 打包
npx tauri build

# 公证（上传 Apple 审核）
xcrun notarytool submit \
  src-tauri/target/release/bundle/dmg/parsing-core_*.dmg \
  --apple-id your@email.com \
  --team-id TEAMID \
  --password "@keychain:AC_PASSWORD" \
  --wait

# 装订票据
xcrun stapler staple \
  src-tauri/target/release/bundle/dmg/parsing-core_*.dmg
```

### 4. 验证

```bash
spctl -a -v src-tauri/target/release/bundle/dmg/parsing-core_*.dmg
# 期望输出: accepted source=Notarized Developer ID
```

## CI 集成

需在 GitHub Secrets 中配置：
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`
- `APPLE_SIGNING_IDENTITY`
- 以 base64 编码的 `.p12` 证书文件
