# Deploying the rentals pipeline

**Live deployment: GitHub Actions** — `.github/workflows/scrape.yml` runs a
cycle every 30 minutes (repo must be public for free unlimited minutes; the
DB persists on the `db-state` branch; it publishes to `caleta-web` with the
`CALETA_TOKEN` secret). Config: `config.ci.yaml`. Secrets: `SMTP_*`,
`ALERT_*`, `FAVS_API`, `FAVS_KEY`, `CALETA_TOKEN`.

The Windows Task Scheduler setup below is kept as a **disabled fallback** on
the laptop. To fall back: `Enable-ScheduledTask -TaskName "DaftWatch Rentals"`
and disable the Actions workflow.

---

This runs `python -m daftwatch run` every 30 minutes with Windows Task
Scheduler. One cycle scrapes the daft.ie sharing searches, detail-fetches new
cheap candidates, upserts SQLite, writes `listings.json`, `git push`es it to a
`caleta-web` checkout, and emails a curated digest.

Files here:

| File | Purpose |
|---|---|
| `run-rentals.bat` | Loads `.env`, runs one cycle, appends to `data\rentals.log` |
| `run-hidden.vbs` | Starts `run-rentals.bat` with no console window; the task runs this |
| `DaftWatch-Rentals.xml` | Task Scheduler task (30-min repeat, forever, hidden) |

## Prerequisites (once)

1. **Install the package and browser:**
   ```
   pip install -e .
   playwright install chromium
   ```
   `pip install -e .` is what makes `python -m daftwatch` work. Use the same
   Python that `run-rentals.bat` calls (the global interpreter on `PATH`).

2. **Fill `.env`** (copy from `.env.example`). SMTP host/port/user/pass,
   `ALERT_FROM`, `ALERT_TO`. The `.bat` parses it line by line, so keep it to
   plain `KEY=VALUE` lines — do **not** put trailing `# comments` on a value
   line (they would be swallowed into the value).

   Optional: `FAVS_API` (`https://www.caleta.tech/api/favs`) + `FAVS_KEY` (the
   dashboard's favourites passphrase) let the digest add a "watched room
   updated / back on the market" section that ignores the price/distance
   filters.

3. **Create `data\config.yaml`:**
   ```
   mkdir data
   copy config.example.yaml data\config.yaml
   ```
   Then edit the `publish:` block so `repo_dir` / `json_path` point at a real
   local `caleta-web` checkout that has push rights:
   ```yaml
   publish:
     json_path: "D:/Github/caleta-web/dashboards/rentals/listings.json"
     repo_dir:  "D:/Github/caleta-web"
     file_rel:  "dashboards/rentals/listings.json"
     git_push:  true
   ```

4. **Make sure git can push `caleta-web` without a prompt** — an SSH key with
   no passphrase, or a configured credential helper. Test it:
   `git -C D:\Github\caleta-web push`. If that ever asks for a password, the
   scheduled task will hang until its 2-hour limit.

## First run — do this by hand

```
deploy\run-rentals.bat
```

The **first** cycle detail-fetches every share priced at or below EUR 800
across Cork, Limerick and Dublin — roughly 100-150 detail pages, about
20-30 minutes. `detail_max_per_cycle` (default 60) caps each cycle, so if you
prefer you can just let the first two or three scheduled runs warm the cache.
Every cycle after the cache is warm is fast — it only detail-fetches listings
it has never seen.

Watch it: `type data\rentals.log`.

## Import the scheduled task

> Already registered on this machine as **"DaftWatch Rentals"** (30-min repeat
> forever, interactive logon, no console window). The steps below are for
> reinstalling or another machine.

Register it from **PowerShell** in `D:\Github\daft-watch` (no admin needed for
an `InteractiveToken` task). This is the reliable path — `schtasks /xml`
chokes on the file encoding:

```powershell
$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"D:\Github\daft-watch\deploy\run-hidden.vbs"'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 30)
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
$s.DisallowStartIfOnBatteries = $false      # it's a laptop
$s.StopIfGoingOnBatteries = $false
Register-ScheduledTask -TaskName 'DaftWatch Rentals' -Action $a -Trigger $t -Principal $p -Settings $s -Force
```

A `-RepetitionInterval` with no `-RepetitionDuration` repeats forever. Do not
use `schtasks /change /tr` on the live task — it rewrites the trigger with a
33-day duration and the repeat silently stops.

Or import `deploy\DaftWatch-Rentals.xml` (saved as **UTF-16**) via **Task
Scheduler → Action → Import Task…**.

The task runs as the current interactive user (`InteractiveToken`) — it only
fires while you are logged on, and needs no stored password. It will not stack
runs (`IgnoreNew`); a cycle still running when the next trigger fires is
skipped. Each run is capped at 2 hours.

## Change the interval

- Task Scheduler → the task → **Triggers** tab → edit the trigger →
  **Repeat task every:** change from 30 minutes.
- Or edit `<Interval>PT30M</Interval>` in `DaftWatch-Rentals.xml`, delete the
  task (`schtasks /delete /tn "DaftWatch Rentals" /f`), and re-import.

Also set `interval_minutes` in `data\config.yaml` to match, so any internal
timing stays consistent.

## Logs

Everything (stdout + stderr) is appended to `data\rentals.log`. It **grows
forever** — there is no rotation. Truncate it every so often:

```
type nul > data\rentals.log
```

(Log rotation / `logrotate` is out of scope here.)

## Disable / remove

```
schtasks /change /tn "DaftWatch Rentals" /disable      REM pause
schtasks /delete  /tn "DaftWatch Rentals" /f           REM remove
```

## Troubleshooting

Check `data\rentals.log` first — everything lands there.

| Symptom | Likely cause |
|---|---|
| SMTP error 525 / auth failure | Brevo (Sendinblue) needs the sending IP on its allowlist. Add this machine's public IP in the Brevo dashboard. |
| No email, `listings.json` empty or unchanged | Look for `AdapterError` or `RateLimited` in the log — daft.ie's Cloudflare challenge is blocking the headless browser. Usually transient; it retries next cycle. Persistent = see the "If it stops finding listings" section in the main README. |
| `git_publish failed` in the log | The `caleta-web` checkout at `publish.repo_dir` can't commit or push — wrong path, dirty/detached tree, or push needs a password. Run `git -C <repo_dir> status` and `git -C <repo_dir> push` by hand. A publish failure is logged and swallowed; the email still goes out. |
| Task shows "Running" for hours | A git push is waiting on a credential prompt, or a detail fetch stalled. The 2-hour `ExecutionTimeLimit` will kill it; fix the credential helper. |
| Task never runs | You are not logged on (this task needs an interactive session), or it's disabled. |
