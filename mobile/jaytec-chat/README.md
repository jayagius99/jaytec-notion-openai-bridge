# JAYTEC Chat Android

Native Android sideload client for the JAYTEC/AI gateway.

## Configure on first launch
Open **Settings** and enter:
- Gateway base URL
- Bearer/access token
- Optional LAN HTTP developer toggle for private/local gateway testing

Tap **Save**, then **Test Connection**.

The client expects:
- GET /v1/health
- POST /v1/chat/messages
- GET /v1/chat/events?conversation_id=<id>&after_seq=<seq>

Foreground chat reconciliation runs once per second. Chat/session state and sequence cursor survive app restarts. Bearer token is encrypted using Android Keystore before being stored locally.

The APK produced by CI is a debug-signed sideload build named JAYTEC-Chat.apk.
