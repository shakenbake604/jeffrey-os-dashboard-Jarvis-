#!/usr/bin/env python3
"""Generate state.json for Jeffrey OS Dashboard v6 — real-time system + project + PR + weather data"""
import json, subprocess, os, time, platform, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CDT = timezone(timedelta(hours=-6))
now = datetime.now(CDT)

PROJECTS_DIR = Path.home() / "jeffrey/workspace/projects"
GARDEN_WORLD_STATE = PROJECTS_DIR / "ai-garden/experiments/world-state.json"
OUTPUT = PROJECTS_DIR / "jeffrey-os-dashboard/state.json"
PUBLIC_OUTPUT = PROJECTS_DIR / "jeffrey-os-dashboard/api/state.json"

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def get_all_projects():
    """Scan all git projects and return real-time status."""
    projects = {}
    if not PROJECTS_DIR.is_dir():
        return projects
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue
        name = d.name
        if name == "jeffrey-os-dashboard":
            continue  # skip self
        raw = run(f"cd '{d}' && git log --format='%H|%s|%aI|%ar' -1 2>/dev/null")
        parts = raw.split("|", 3) if raw else []
        branch = run(f"cd '{d}' && git branch --show-current 2>/dev/null")
        dirty = run(f"cd '{d}' && git status --porcelain 2>/dev/null")
        commit_iso = parts[2] if len(parts) > 2 else ""
        # Determine health: green (<24h), yellow (<7d), red (>7d)
        health = "stale"
        if commit_iso:
            try:
                commit_dt = datetime.fromisoformat(commit_iso)
                age_hours = (now - commit_dt).total_seconds() / 3600
                if age_hours < 24:
                    health = "active"
                elif age_hours < 168:
                    health = "recent"
                else:
                    health = "stale"
            except Exception:
                pass
        projects[name] = {
            "branch": branch or "unknown",
            "lastCommitHash": parts[0][:8] if parts else "",
            "lastCommitMsg": parts[1] if len(parts) > 1 else "",
            "lastCommitTime": commit_iso,
            "lastCommitAgo": parts[3] if len(parts) > 3 else "",
            "dirty": len(dirty.splitlines()) if dirty else 0,
            "health": health,
        }
    return projects

def get_crons():
    raw = run("openclaw cron list --json 2>/dev/null", timeout=8)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        result = []
        for c in jobs:
            sched = c.get("schedule", {})
            state = c.get("state", {})
            sched_str = ""
            if sched.get("kind") == "every":
                mins = sched.get("everyMs", 0) // 60000
                if mins >= 60:
                    sched_str = f"every {mins // 60}h"
                else:
                    sched_str = f"every {mins}m"
            elif sched.get("kind") == "at":
                sched_str = f"once @ {sched.get('at', '?')}"
            result.append({
                "name": c.get("name", ""),
                "description": c.get("description", ""),
                "schedule": sched_str,
                "enabled": c.get("enabled", True),
                "lastStatus": state.get("lastStatus", ""),
                "lastDurationMs": state.get("lastDurationMs", 0),
                "nextRun": state.get("nextRunAtMs", 0),
                "consecutiveErrors": state.get("consecutiveErrors", 0),
            })
        return result
    except Exception:
        return []

def get_garden_stats():
    try:
        with open(GARDEN_WORLD_STATE) as f:
            d = json.load(f)
        return {
            "plants": len(d.get("plants", [])),
            "citizens": len(d.get("citizens", [])),
            "factions": len(d.get("factions", [])),
            "threats": len(d.get("threats", [])),
            "events": len(d.get("events", [])),
            "version": d.get("version", 0),
            "structures": len(d.get("structuresBuilt", [])),
            "lastUpdated": d.get("lastUpdated", ""),
        }
    except Exception:
        return {}

def get_system_metrics():
    """CPU load, memory, disk."""
    metrics = {}
    # Load averages
    try:
        load = os.getloadavg()
        metrics["loadAvg"] = [round(x, 2) for x in load]
    except Exception:
        metrics["loadAvg"] = []
    # Memory via vm_stat (macOS)
    vm = run("vm_stat 2>/dev/null")
    if vm:
        try:
            lines = vm.splitlines()
            page_size = 16384  # default Apple Silicon
            stats = {}
            for line in lines[1:]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    val = val.strip().rstrip(".")
                    try:
                        stats[key.strip()] = int(val)
                    except ValueError:
                        pass
            free_pages = stats.get("Pages free", 0) + stats.get("Pages speculative", 0)
            active = stats.get("Pages active", 0)
            inactive = stats.get("Pages inactive", 0)
            wired = stats.get("Pages wired down", 0)
            compressed = stats.get("Pages occupied by compressor", 0)
            total_used = (active + wired + compressed) * page_size
            total_free = (free_pages + inactive) * page_size
            total = total_used + total_free
            metrics["memUsedGB"] = round(total_used / (1024**3), 1)
            metrics["memTotalGB"] = round(total / (1024**3), 1)
            metrics["memPercent"] = round(total_used / total * 100, 1) if total else 0
        except Exception:
            pass
    # Disk
    disk = run("df -h / 2>/dev/null | tail -1")
    if disk:
        parts = disk.split()
        if len(parts) >= 5:
            metrics["diskTotal"] = parts[1]
            metrics["diskUsed"] = parts[2]
            metrics["diskAvail"] = parts[3]
            metrics["diskPercent"] = parts[4]
    return metrics

def get_uptime():
    raw = run("uptime")
    return raw

def get_github_prs():
    """Fetch open GitHub PRs authored by juliosuas using gh CLI.

    Use `gh search prs` instead of `gh pr list`: this dashboard is a public
    operating view, not just the current repo. `gh pr list` only sees one repo
    and made the GitHub Pages build look empty outside the local network.
    """
    try:
        raw = run(
            "gh search prs --author juliosuas --state open --limit 50 "
            "--json number,title,state,repository,url,updatedAt 2>/dev/null",
            timeout=15
        )
        if not raw:
            return []
        prs = json.loads(raw)
        return prs if isinstance(prs, list) else []
    except Exception as e:
        print(f"PR fetch failed: {e}")
        return []

def get_weather():
    """Fetch weather for Mexico City and Acapulco from wttr.in."""
    result = {}
    cities = {"cdmx": "Mexico+City", "acapulco": "Acapulco"}
    for key, city in cities.items():
        try:
            url = f"https://wttr.in/{city}?format=j1"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            cur = data["current_condition"][0]
            result[key] = {
                "temp": cur.get("temp_C", "?"),
                "feelsLike": cur.get("FeelsLikeC", "?"),
                "humidity": cur.get("humidity", "?"),
                "condition": cur.get("weatherDesc", [{}])[0].get("value", "Unknown"),
                "windKph": cur.get("windspeedKmph", "?"),
                "icon": _weather_icon(cur.get("weatherCode", "113")),
                "updated": now.isoformat(),
            }
        except Exception as e:
            print(f"Weather fetch failed for {city}: {e}")
            result[key] = None
    return result

def _weather_icon(code):
    """Map wttr.in weather code to emoji icon."""
    code = str(code)
    sunny = {"113"}
    partly = {"116", "119"}
    overcast = {"122"}
    fog = {"143", "248", "260"}
    rain = {"176", "185", "263", "266", "281", "284", "293", "296", "299", "302", "305", "308", "311", "314", "317", "320", "353", "356", "359", "362", "365"}
    thunder = {"200", "386", "389", "392", "395"}
    snow = {"179", "182", "323", "326", "329", "332", "335", "338", "368", "371", "374", "377"}
    if code in sunny: return "☀️"
    if code in partly: return "⛅"
    if code in overcast: return "☁️"
    if code in fog: return "🌫️"
    if code in thunder: return "⛈️"
    if code in snow: return "❄️"
    if code in rain: return "🌧️"
    return "🌤️"

def get_openclaw_status():
    """Check OpenClaw system health. Cached for 5 min — `doctor` is expensive."""
    cache_path = "/tmp/openclaw-doctor-cache.json"
    try:
        if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path)) < 300:
            with open(cache_path) as f:
                return json.load(f)
    except Exception:
        pass
    try:
        raw = run("~/homebrew/bin/openclaw doctor --non-interactive 2>&1", timeout=15)
        if not raw:
            result = {"status": "unknown", "raw": ""}
        else:
            ok = "all systems" in raw.lower() or "healthy" in raw.lower() or "ok" in raw.lower()
            result = {"status": "ok" if ok else "warn", "raw": raw[:500]}
    except Exception as e:
        result = {"status": "error", "error": str(e)}
    try:
        with open(cache_path, "w") as f:
            json.dump(result, f)
    except Exception:
        pass
    return result

# Build state — parallelize
with ThreadPoolExecutor(max_workers=8) as pool:
    f_projects = pool.submit(get_all_projects)
    f_crons = pool.submit(get_crons)
    f_garden = pool.submit(get_garden_stats)
    f_metrics = pool.submit(get_system_metrics)
    f_uptime = pool.submit(get_uptime)
    f_hostname = pool.submit(run, "hostname")
    f_prs = pool.submit(get_github_prs)
    f_weather = pool.submit(get_weather)
    f_oc_status = pool.submit(get_openclaw_status)

state = {
    "timestamp": now.isoformat(),
    "hostname": f_hostname.result(),
    "uptime": f_uptime.result(),
    "system": f_metrics.result(),
    "garden": f_garden.result(),
    "projects": f_projects.result(),
    "crons": f_crons.result(),
    "prs": f_prs.result(),
    "weather": f_weather.result(),
    "openclawStatus": f_oc_status.result(),
    "infrastructure": {
        "host": "Mac mini M4",
        "ip": run("ipconfig getifaddr en0") or run("ipconfig getifaddr en1") or "unknown",
        "os": f"macOS {platform.mac_ver()[0]}",
        "arch": platform.machine(),
        "dashboardPort": 8080,
    },
    "tokens": {
        "plan": "Claude Max 5x",
        "promo": "2x until March 27",
        "model_main": "claude-opus-4-6",
        "model_crons": "claude-sonnet-4",
    },
}

with open(OUTPUT, "w") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

PUBLIC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
public_state = json.loads(json.dumps(state))
public_state.setdefault("infrastructure", {})["ip"] = "local-only"
with open(PUBLIC_OUTPUT, "w") as f:
    json.dump(public_state, f, indent=2, ensure_ascii=False)

gs = state["garden"]
proj_count = len(state["projects"])
active_count = sum(1 for p in state["projects"].values() if p["health"] == "active")
pr_count = len(state["prs"])
cdmx_weather = state["weather"].get("cdmx") or {}
print(f"State updated: {now.strftime('%Y-%m-%d %H:%M:%S')} CDT")
print(f"Projects: {proj_count} total, {active_count} active today")
print(f"Garden: v{gs.get('version',0)} | {gs.get('citizens',0)} citizens, {gs.get('plants',0)} plants")
print(f"GitHub PRs: {pr_count} open")
print(f"Weather CDMX: {cdmx_weather.get('temp','?')}°C {cdmx_weather.get('condition','')}")
