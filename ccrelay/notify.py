"""Plain notifications: a Windows toast from WSL when powershell.exe is available, always a log line."""
import base64
import os
import shutil
import subprocess
import time

from . import store

TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:RELAY_TITLE)) > $null
$t.Item(1).AppendChild($xml.CreateTextNode($env:RELAY_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def toast_env(title, body, base=None):
    """Environment for powershell.exe. WSLENV's /w flag passes a variable from WSL to Windows
    (/u is the other direction); any existing WSLENV entries are kept."""
    env = dict(os.environ if base is None else base)
    shared = "RELAY_TITLE/w:RELAY_BODY/w"
    env["WSLENV"] = f"{env['WSLENV']}:{shared}" if env.get("WSLENV") else shared
    env["RELAY_TITLE"], env["RELAY_BODY"] = title, body
    return env


def notify(db, title, body, handoff_id=None):
    title = store.load_config().get("notify_title_prefix", "") + title  # set by the acceptance environment
    store.event(db, "notify", f"{title}: {body}", handoff_id)
    with open(store.relay_home() / "notifications.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {title}: {body}\n")
    ps = shutil.which("powershell.exe")
    if not ps:
        return
    encoded = base64.b64encode(TOAST.encode("utf-16-le")).decode()
    try:
        subprocess.Popen([ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                         env=toast_env(title, body),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        store.event(db, "notify_failed", str(e), handoff_id)
