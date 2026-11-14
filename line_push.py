"""LINE Messaging API 推播（LINE Notify 已於 2025/3/31 停止服務，須改用官方帳號 + Messaging API）。
圖片訊息只能給 HTTPS 網址，所以要先把 PNG 放到可公開存取的位置。"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import time

import requests

log = logging.getLogger(__name__)


def _gh_upload(path: str, branch: str) -> str:
    """用 GitHub Contents API 把圖放到同一 repo 的指定分支，回傳 raw 網址。
    只有 public repo 的 raw 網址 LINE 才讀得到。"""
    repo, tok = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    api = f"https://api.github.com/repos/{repo}"
    hd = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    if requests.get(f"{api}/git/ref/heads/{branch}", headers=hd, timeout=30).status_code == 404:
        base = os.environ.get("GITHUB_REF_NAME", "main")
        sha = requests.get(f"{api}/git/ref/heads/{base}", headers=hd, timeout=30).json()["object"]["sha"]
        requests.post(f"{api}/git/refs", headers=hd, timeout=30,
                      json={"ref": f"refs/heads/{branch}", "sha": sha}).raise_for_status()
    name = f"plumes/{os.path.basename(path)}"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    requests.put(f"{api}/contents/{name}", headers=hd, timeout=60,
                 json={"message": f"plume {name}", "content": b64, "branch": branch}
                 ).raise_for_status()
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"


def upload_image(path: str, cfg: dict) -> str | None:
    h = cfg.get("image_host", {})
    kind = h.get("type", "none")
    try:
        if kind == "imgur" and h.get("imgur_client_id"):
            with open(path, "rb") as f:
                r = requests.post("https://api.imgur.com/3/image",
                                  headers={"Authorization": f"Client-ID {h['imgur_client_id']}"},
                                  data={"image": base64.b64encode(f.read()), "type": "base64"},
                                  timeout=60)
            r.raise_for_status()
            return r.json()["data"]["link"]
        if kind == "github":
            return _gh_upload(path, h.get("github_branch", "plume-images"))
        if kind == "static" and h.get("static_dir") and h.get("static_base_url"):
            name = f"{int(time.time())}_{os.path.basename(path)}"
            shutil.copy(path, os.path.join(h["static_dir"], name))
            return h["static_base_url"].rstrip("/") + "/" + name
    except Exception as e:  # noqa: BLE001
        log.warning("圖片上傳失敗：%s", e)
    return None


def push(cfg: dict, text: str, img_url: str | None = None, preview_url: str | None = None,
         dry_run: bool = False, admin: bool = False) -> None:
    """admin=True：發給 line.admin_to（管理者個人），未設定時退回 line.to。
    群組訊息以「群組人數」計算則數，運作確認與測試訊息建議只發給管理者個人。"""
    msgs = [{"type": "text", "text": text[:4900]}]
    if img_url:
        msgs.append({"type": "image", "originalContentUrl": img_url,
                     "previewImageUrl": preview_url or img_url})
    if dry_run:
        print(f"=== DRY RUN：LINE 訊息（{'管理者' if admin else '通報對象'}）===\n" + text + (f"\n[圖片] {img_url}" if img_url else ""))
        return
    lc = cfg["line"]
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      headers={"Authorization": f"Bearer {lc['channel_access_token']}",
                               "Content-Type": "application/json"},
                      json={"to": (lc.get("admin_to") if admin else None) or lc["to"],
                            "messages": msgs}, timeout=30)
    if r.status_code != 200:
        log.error("LINE 推播失敗 %s：%s", r.status_code, r.text)
        r.raise_for_status()
