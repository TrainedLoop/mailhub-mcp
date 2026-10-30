from pathlib import Path

from mailhub_mcp.providers.gmail.provider import SCOPES


def authorize(client_secret: Path) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    return creds.to_json()
