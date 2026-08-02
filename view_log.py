#!/usr/bin/env python3
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

_USE_COLOR = sys.stdout.isatty()
_R  = "\033[91m"; _Y  = "\033[93m"; _C  = "\033[96m"
_DM = "\033[2m"; _B  = "\033[1m"; _E  = "\033[0m"

def _c(code, text): return f"{code}{text}{_E}" if _USE_COLOR else text
def colorize(line):
    if not _USE_COLOR: return line
    if "| ERROR   |" in line: return _c(_R, line)
    if "| WARNING |" in line: return _c(_Y, line)
    if "| DEBUG   |" in line: return _c(_DM, line)
    if "| INFO    |" in line and any(tok in line for tok in ["✅","Sent","State saved","Run complete"]):
        return _c(_C, line)
    return line

def read_all_lines(log_path):
    lines = []
    for path in [Path(str(log_path) + ".1"), log_path]:
        if path.exists():
            with open(path, encoding="utf-8", errors="replace") as f:
                lines.extend(f.readlines())
    return lines

def passes_filters(line, args):
    if not line.strip(): return False
    if args.errors:
        if "| WARNING |" not in line and "| ERROR   |" not in line: return False
    elif args.level:
        if f"| {args.level:<8} |" not in line: return False
    if args.tweet and args.tweet not in line: return False
    if args.keyword and args.keyword.lower() not in line.lower(): return False
    if args.today:
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%dT")
        if not line.startswith(today_prefix): return False
    return True

def print_statistics(lines, header=""):
    counts = {"DEBUG":0,"INFO":0,"WARNING":0,"ERROR":0}
    sent = important = non_important = groq_errors = tg_errors = state_saves = acct_loads = duplicates = 0
    first_ts = last_ts = ""
    for line in lines:
        for lvl in counts:
            if f"| {lvl:<8} |" in line: counts[lvl] += 1
        if len(line)>=20 and "T" in line[:11]:
            ts=line[:20].strip()
            if not first_ts: first_ts=ts
            last_ts=ts
        low=line.lower()
        if "✅ sent to" in low or "✅ sent:" in low: sent+=1
        if "→ important" in low and "non_important" not in low: important+=1
        if "→ non_important" in low: non_important+=1
        if "groq" in low and ("error" in low or "timed out" in low): groq_errors+=1
        if "telegram api rejected" in low or "failed to send after" in low: tg_errors+=1
        if "state saved" in low: state_saves+=1
        if "account" in low and "loaded" in low: acct_loads+=1
        if "duplicate" in low or "skipped" in low: duplicates+=1
    sep="━"*54
    w=_B if _USE_COLOR else ""
    e=_E if _USE_COLOR else ""
    print(f"\n{sep}\n{w}📊  Log Statistics{e}  {header}\n{sep}")
    print(f"  Total lines:          {len(lines):>10,}")
    print(f"  Period:               {first_ts or 'n/a'}  →  {last_ts or 'n/a'}")
    print(f"{'─'*54}")
    print(f"  DEBUG:                {counts['DEBUG']:>10,}")
    print(f"  INFO:                 {counts['INFO']:>10,}")
    print(f"  WARNING:              {counts['WARNING']:>10,}")
    print(f"  ERROR:                {counts['ERROR']:>10,}")
    print(f"{'─'*54}")
    print(f"  Tweets sent:          {sent:>10,}")
    print(f"  → Important:          {important:>10,}")
    print(f"  → Non-Important:      {non_important:>10,}")
    print(f"  Duplicates/skipped:   {duplicates:>10,}")
    print(f"  Groq errors:          {groq_errors:>10,}")
    print(f"  Telegram errors:      {tg_errors:>10,}")
    print(f"  State saves:          {state_saves:>10,}")
    print(f"  Account loads:        {acct_loads:>10,}")
    print(f"{sep}\n")

def build_parser():
    p=argparse.ArgumentParser(description="View and filter bot log files.")
    p.add_argument("-f","--file",default="main_bot.log")
    p.add_argument("-n","--lines",type=int,default=50)
    p.add_argument("-l","--level",choices=["DEBUG","INFO","WARNING","ERROR"])
    p.add_argument("-k","--keyword")
    p.add_argument("-t","--tweet")
    p.add_argument("--errors",action="store_true")
    p.add_argument("--today",action="store_true")
    p.add_argument("--stats",action="store_true")
    return p

def main():
    args=build_parser().parse_args()
    log_path=Path(args.file)
    backup=Path(str(log_path)+".1")
    if not log_path.exists() and not backup.exists():
        print(f"❌ Log file not found: {log_path}"); sys.exit(1)
    all_lines=read_all_lines(log_path)
    if not all_lines:
        print(f"📋 {log_path} is empty."); return
    if args.stats:
        print_statistics(all_lines, header=str(log_path))
    filtered=[l for l in all_lines if passes_filters(l, args)]
    display=filtered[-args.lines:] if args.lines>0 else filtered
    active=[]
    if args.errors: active.append("level≥WARNING")
    if args.level: active.append(f"level={args.level}")
    if args.keyword: active.append(f"keyword={args.keyword!r}")
    if args.tweet: active.append(f"tweet_id={args.tweet}")
    if args.today: active.append("today only")
    filter_desc=" | ".join(active) if active else "none"
    sep="━"*72
    print(f"\n{sep}\n📋  {args.file}   total={len(all_lines):,}   matched={len(filtered):,}   showing={len(display)}\n    Filters: {filter_desc}\n{sep}\n")
    if not display:
        print("  (no lines match the active filters)\n"); return
    for line in display:
        print(colorize(line), end="" if line.endswith("\n") else "\n")

if __name__=="__main__":
    main()
