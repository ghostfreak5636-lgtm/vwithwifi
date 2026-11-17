#!/usr/bin/env python3
"""
Run this ONCE (works in Pydroid) to get YT_REFRESH_TOKEN for the channel
you want ClipBot to upload to.

Steps:
 1. Paste your OAuth Client ID + Client Secret (type: Desktop app).
 2. Open the printed link, log in, and PICK THE NEW CLIPS CHANNEL.
 3. The page will fail to load (localhost) - that's expected.
    Copy the full address from the browser bar and paste it here.
"""
import sys
from urllib.parse import urlencode, urlparse, parse_qs, unquote

import requests

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT = "http://localhost:8080"


def main():
    client_id = input("Client ID: ").strip()
    client_secret = input("Client secret: ").strip()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    print("\nOpen this link, sign in and choose the channel:\n")
    print(url)
    print("\nAfter approving, the page will fail to load. That's fine.")
    pasted = input("\nPaste the full address from the browser bar: ").strip()

    code = parse_qs(urlparse(pasted).query).get("code", [None])[0]
    if not code:
        code = unquote(pasted)  # in case only the code was pasted

    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    data = r.json()
    if "refresh_token" not in data:
        print("\nNo refresh token returned:", data)
        print("Tip: remove the app's access at myaccount.google.com/permissions and try again.")
        sys.exit(1)

    print("\n=== Add these as GitHub repo secrets ===")
    print("YT_CLIENT_ID     =", client_id)
    print("YT_CLIENT_SECRET =", client_secret)
    print("YT_REFRESH_TOKEN =", data["refresh_token"])


if __name__ == "__main__":
    main()
