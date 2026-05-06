#!/usr/bin/env python3
"""
Golf course scorecard scraper.

Reads a course list file, scrapes scorecard data from 18Birdies
(with golfscorekeeper.com as fallback), and upserts to Supabase.

Usage:
    python scrape_courses.py <course-list.txt> [--limit N] [--dry-run]

Environment variables:
    SUPABASE_URL   - e.g. https://mgjhzgadexbhkawyxhjv.supabase.co
    SUPABASE_KEY   - service role key (not anon key)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from supabase import create_client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://mgjhzgadexbhkawyxhjv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DELAY = 2.0          # seconds between requests — be polite
BATCH_SIZE = 25      # upsert to Supabase every N courses

TODAY = date.today().isoformat()

# ---------------------------------------------------------------------------
# Course list parsing
# ---------------------------------------------------------------------------

def load_course_list(path: str) -> list[dict]:
    courses = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            continue
        name = parts[0]
        city_state = parts[1]
        city = city_state.split(",")[0].strip()
        state = city_state.split(",")[1].strip() if "," in city_state else "MN"
        num_holes = int(parts[3])
        courses.append({"name": name, "city": city, "state": state, "num_holes": num_holes})
    return courses

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_existing_courses(supabase, state: str) -> set[tuple]:
    result = supabase.table("courses").select("name,city").eq("state", state).execute()
    return {(r["name"], r["city"]) for r in result.data}

def upsert_courses(supabase, rows: list[dict], dry_run: bool):
    if dry_run:
        print(f"  [dry-run] would upsert {len(rows)} rows")
        return
    supabase.table("courses").upsert(rows, on_conflict="name,city").execute()

# ---------------------------------------------------------------------------
# Search — find 18Birdies URL via DuckDuckGo
# ---------------------------------------------------------------------------

def find_18birdies_url(course_name: str, city: str, state: str) -> str | None:
    query = f"site:18birdies.com {course_name} {city} {state} scorecard"
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select(".result__a"):
            href = a.get("href", "")
            if "18birdies.com/golf-courses/club/" in href:
                # DuckDuckGo wraps URLs — extract the real one
                match = re.search(r"uddg=([^&]+)", href)
                if match:
                    from urllib.parse import unquote
                    return unquote(match.group(1))
                return href
    except Exception as e:
        print(f"    search error: {e}")
    return None

def find_golfscorekeeper_url(course_name: str, city: str, state: str) -> str | None:
    query = f"site:golfscorekeeper.com {course_name} {city} {state}"
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select(".result__a"):
            href = a.get("href", "")
            if "golfscorekeeper.com" in href:
                match = re.search(r"uddg=([^&]+)", href)
                if match:
                    from urllib.parse import unquote
                    return unquote(match.group(1))
    except Exception as e:
        print(f"    search error: {e}")
    return None

# ---------------------------------------------------------------------------
# 18Birdies scraper
# ---------------------------------------------------------------------------

def scrape_18birdies(url: str, expected_city: str, expected_state: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"    fetch error: {e}")
        return None

    if r.status_code != 200:
        print(f"    HTTP {r.status_code}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # 18Birdies is a React SPA — scorecard data is embedded in a <script> tag
    # as part of window.__INITIAL_STATE__ or a next.js __NEXT_DATA__ block
    for script in soup.find_all("script"):
        text = script.string or ""

        # Next.js apps embed data here
        if "__NEXT_DATA__" in text or (script.get("id") == "__NEXT_DATA__"):
            try:
                raw = script.string or text
                data = json.loads(raw)
                return parse_nextjs_course_data(data, url, expected_city, expected_state)
            except Exception:
                pass

        # Generic window.__INITIAL_STATE__
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return parse_initial_state(data, url, expected_city, expected_state)
            except Exception:
                pass

    # Fallback: look for JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") in ("SportsClub", "GolfCourse"):
                return parse_jsonld(data, url, expected_city, expected_state)
        except Exception:
            pass

    # Fallback: try to find the club ID in the URL and call 18Birdies API
    match = re.search(r"/club/(\d+)/", url)
    if match:
        club_id = match.group(1)
        return fetch_18birdies_api(club_id, url, expected_city, expected_state)

    return None


def fetch_18birdies_api(club_id: str, source_url: str, city: str, state: str) -> dict | None:
    """Try 18Birdies internal API endpoints."""
    api_urls = [
        f"https://18birdies.com/api/v2/courses/{club_id}/scorecard",
        f"https://18birdies.com/api/courses/{club_id}",
        f"https://api.18birdies.com/v3/courses/{club_id}/scorecard",
    ]
    for api_url in api_urls:
        try:
            r = requests.get(api_url, headers={**HEADERS, "Accept": "application/json"}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return parse_api_response(data, source_url, city, state)
        except Exception:
            pass
    return None


def parse_nextjs_course_data(data: dict, url: str, city: str, state: str) -> dict | None:
    """Parse Next.js __NEXT_DATA__ structure — adjust key paths as needed."""
    try:
        props = data.get("props", {}).get("pageProps", {})
        course = props.get("course") or props.get("courseData") or props.get("club")
        if not course:
            return None
        return build_course_record(course, url, city, state)
    except Exception:
        return None


def parse_initial_state(data: dict, url: str, city: str, state: str) -> dict | None:
    try:
        course = (
            data.get("course")
            or data.get("courseDetail")
            or data.get("currentCourse")
        )
        if not course:
            return None
        return build_course_record(course, url, city, state)
    except Exception:
        return None


def parse_jsonld(data: dict, url: str, city: str, state: str) -> dict | None:
    # JSON-LD rarely has hole-level scorecard data — skip if no tee info
    return None


def parse_api_response(data: dict, url: str, city: str, state: str) -> dict | None:
    try:
        return build_course_record(data, url, city, state)
    except Exception:
        return None


def build_course_record(course: dict, source_url: str, city: str, state: str) -> dict | None:
    """
    Convert a raw course dict (from any 18Birdies data source) into the
    Supabase row format.  Key names here are guesses — update after inspecting
    the actual JSON structure with --dry-run + print(course.keys()).
    """
    tee_boxes = (
        course.get("teeBoxes")
        or course.get("tee_boxes")
        or course.get("tees")
        or []
    )
    if not tee_boxes:
        return None

    tees = []
    primary = None

    for tee in tee_boxes:
        color = tee.get("teeName") or tee.get("tee_name") or tee.get("color") or "Unknown"
        rating = tee.get("courseRating") or tee.get("course_rating") or tee.get("rating")
        slope = tee.get("slopeRating") or tee.get("slope_rating") or tee.get("slope")
        total_yards = tee.get("totalYards") or tee.get("total_yards") or tee.get("yardage")
        total_par = tee.get("totalPar") or tee.get("total_par") or tee.get("par")
        holes_raw = tee.get("holes") or tee.get("holeDetails") or []

        holes = []
        for h in holes_raw:
            hole_num = h.get("holeNumber") or h.get("hole_number") or h.get("number")
            par = h.get("par")
            hcp = h.get("handicap") or h.get("handicapIndex") or h.get("handicap_index")
            yards = h.get("yards") or h.get("yardage") or h.get("distance")
            holes.append([hole_num, par, hcp, yards])

        tee_entry = {
            "color": color,
            "rating": float(rating) if rating else None,
            "slope": int(slope) if slope else None,
            "total_yards": int(total_yards) if total_yards else None,
            "total_par": int(total_par) if total_par else None,
            "holes": holes,
        }
        tees.append(tee_entry)

        # Pick middle/primary tee (White or middle index)
        if primary is None or color.lower() in ("white", "blue/white", "middle"):
            primary = tee_entry

    if not primary or not primary["holes"]:
        return None

    hole_pars = [h[1] for h in primary["holes"] if h[1] is not None]
    hole_yardages = [h[3] for h in primary["holes"] if h[3] is not None]

    if not hole_pars:
        return None

    return {
        "name": course.get("name") or course.get("courseName") or course.get("club_name"),
        "city": city,
        "state": state,
        "num_holes": len(primary["holes"]),
        "course_par": primary["total_par"] or sum(hole_pars),
        "hole_pars": hole_pars,
        "hole_yardages": hole_yardages or None,
        "slope": primary["slope"],
        "rating": primary["rating"],
        "tee_data": {
            "tees": tees,
            "source_url": source_url,
            "scraped_at": TODAY,
        },
    }

# ---------------------------------------------------------------------------
# golfscorekeeper.com scraper (static HTML fallback)
# ---------------------------------------------------------------------------

def scrape_golfscorekeeper(url: str, expected_city: str, expected_state: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"    fetch error: {e}")
        return None

    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Verify this is the right course (city/state check)
    page_text = soup.get_text(" ", strip=True).lower()
    if expected_city.lower() not in page_text:
        print(f"    city mismatch — skipping")
        return None

    tees = []
    primary = None

    # golfscorekeeper renders one table per tee with class like "scorecard-table"
    for table in soup.select("table.scorecard, table.scorecard-table, table[class*='scorecard']"):
        tee_color = "Unknown"
        header = table.find_previous("h2") or table.find_previous("h3") or table.find_previous("h4")
        if header:
            tee_color = header.get_text(strip=True)

        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # Parse header row for column order
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        hole_col = next((i for i, h in enumerate(headers) if "hole" in h), 0)
        par_col = next((i for i, h in enumerate(headers) if h == "par"), None)
        hcp_col = next((i for i, h in enumerate(headers) if "hcp" in h or "handicap" in h), None)
        yds_col = next((i for i, h in enumerate(headers) if "yard" in h or "yds" in h or h == "y"), None)

        if par_col is None:
            continue

        holes = []
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) <= par_col:
                continue
            try:
                hole_num = int(cells[hole_col])
            except (ValueError, IndexError):
                continue
            try:
                par = int(cells[par_col])
            except (ValueError, IndexError):
                par = None
            try:
                hcp = int(cells[hcp_col]) if hcp_col is not None else None
            except (ValueError, IndexError):
                hcp = None
            try:
                yards = int(cells[yds_col].replace(",", "")) if yds_col is not None else None
            except (ValueError, IndexError):
                yards = None
            if par:
                holes.append([hole_num, par, hcp, yards])

        if not holes:
            continue

        # Look for rating/slope near the table
        rating, slope = None, None
        nearby = (table.find_previous("p") or BeautifulSoup("", "html.parser"))
        nearby_text = (table.find_previous(class_=re.compile("rating|slope")) or BeautifulSoup("", "html.parser")).get_text()
        m = re.search(r"rating[:\s]+(\d+\.\d+)", nearby_text, re.I)
        if m:
            rating = float(m.group(1))
        m = re.search(r"slope[:\s]+(\d+)", nearby_text, re.I)
        if m:
            slope = int(m.group(1))

        total_yards = sum(h[3] for h in holes if h[3]) or None
        total_par = sum(h[1] for h in holes if h[1])

        tee_entry = {
            "color": tee_color,
            "rating": rating,
            "slope": slope,
            "total_yards": total_yards,
            "total_par": total_par,
            "holes": holes,
        }
        tees.append(tee_entry)
        if primary is None:
            primary = tee_entry

    if not primary or not primary["holes"]:
        return None

    hole_pars = [h[1] for h in primary["holes"] if h[1]]
    hole_yardages = [h[3] for h in primary["holes"] if h[3]]

    # Extract course name from page title
    title = soup.find("h1")
    name = title.get_text(strip=True) if title else None

    return {
        "name": name,
        "city": expected_city,
        "state": expected_state,
        "num_holes": len(primary["holes"]),
        "course_par": primary["total_par"],
        "hole_pars": hole_pars,
        "hole_yardages": hole_yardages or None,
        "slope": primary["slope"],
        "rating": primary["rating"],
        "tee_data": {
            "tees": tees,
            "source_url": url,
            "scraped_at": TODAY,
        },
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_course(course: dict) -> dict | None:
    name, city, state = course["name"], course["city"], course["state"]
    print(f"  Searching 18Birdies...")
    url = find_18birdies_url(name, city, state)
    time.sleep(DELAY)

    if url:
        print(f"  Fetching {url}")
        result = scrape_18birdies(url, city, state)
        time.sleep(DELAY)
        if result:
            if result.get("name") is None:
                result["name"] = name
            return result

    print(f"  Trying golfscorekeeper...")
    url = find_golfscorekeeper_url(name, city, state)
    time.sleep(DELAY)

    if url:
        print(f"  Fetching {url}")
        result = scrape_golfscorekeeper(url, city, state)
        time.sleep(DELAY)
        if result:
            if result.get("name") is None:
                result["name"] = name
            return result

    return None


def main():
    parser = argparse.ArgumentParser(description="Scrape golf course scorecards to Supabase")
    parser.add_argument("course_list", help="Path to course list .txt file")
    parser.add_argument("--limit", type=int, default=None, help="Max courses to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Supabase")
    args = parser.parse_args()

    if not SUPABASE_KEY and not args.dry_run:
        print("ERROR: set SUPABASE_KEY environment variable (service role key)")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if not args.dry_run else None

    courses = load_course_list(args.course_list)
    print(f"Loaded {len(courses)} courses from {args.course_list}")

    if not args.dry_run:
        existing = get_existing_courses(supabase, courses[0]["state"])
        print(f"Already in Supabase: {len(existing)}")
        courses = [c for c in courses if (c["name"], c["city"]) not in existing]
        print(f"Remaining to scrape: {len(courses)}")

    if args.limit:
        courses = courses[: args.limit]

    inserted = updated = failed = 0
    batch = []
    total = len(courses)

    for i, course in enumerate(courses, 1):
        print(f"\n[{i}/{total}] {course['name']} — {course['city']}, {course['state']}")

        result = scrape_course(course)

        if result:
            batch.append(result)
            inserted += 1
            print(f"  OK — par {result.get('course_par')}, {result.get('num_holes')} holes, "
                  f"{len(result.get('tee_data', {}).get('tees', []))} tees")
        else:
            failed += 1
            print(f"  FAILED — no scorecard data found")

        if i % 5 == 0:
            print(f"\nProgress: {i} of {total} | Scraped: {inserted} | Failed: {failed}")

        if len(batch) >= BATCH_SIZE:
            upsert_courses(supabase, batch, args.dry_run)
            print(f"  Upserted batch of {len(batch)}")
            batch = []

    if batch:
        upsert_courses(supabase, batch, args.dry_run)
        print(f"  Upserted final batch of {len(batch)}")

    print(f"\n{'='*50}")
    print(f"FINAL SUMMARY")
    print(f"  Total attempted : {total}")
    print(f"  Inserted/updated: {inserted}")
    print(f"  Failed          : {failed}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
