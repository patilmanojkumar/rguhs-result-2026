import os
import re
import time
from collections import defaultdict, deque
from threading import Lock

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

RGUHS_URL = "https://rguhs.karnataka.gov.in/rguresult/"
HTTP_TIMEOUT = (30, 300)  # 30s connect timeout, 300s (5 minutes) read timeout for slow portal response
app = FastAPI(title="RGUHS Result Helper", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Lightweight in-memory throttling. No registration numbers or result data are stored.
_rate_lock = Lock()
_requests_by_ip = defaultdict(deque)
RATE_WINDOW_SECONDS = 60
RATE_LIMIT = 6

class LookupRequest(BaseModel):
    registration_number: str = Field(min_length=4, max_length=20)
    consent: bool = False

def enforce_rate_limit(ip: str):
    now = time.time()
    with _rate_lock:
        q = _requests_by_ip[ip]
        while q and now - q[0] > RATE_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many lookups from this connection. Please wait one minute and try again."
            )
        q.append(now)

def get_viewstate_from_html(html: str) -> str:
    m = re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', html)
    if not m:
        m = re.search(r'name="__VIEWSTATE"\s+id="__VIEWSTATE"\s+value="([^"]*)"', html)
    if not m:
        raise RuntimeError("The result portal did not provide page state.")
    return m.group(1)

def get_viewstate_from_delta(delta: str) -> str:
    m = re.search(r'\|hiddenField\|__VIEWSTATE\|([^|]*)\|', delta)
    if not m:
        raise RuntimeError("The portal response did not include updated page state.")
    return m.group(1)

def extract_update_panel(delta: str) -> str:
    marker = "updatePanel|upnl|"
    if marker not in delta:
        # Some ASP.NET errors arrive as a different delta record.
        raise RuntimeError("The portal returned an unexpected response. It may be busy; try again shortly.")
    fragment = delta.split(marker, 1)[1]
    fragment = re.split(r'\|\d+\|hiddenField\|__VIEWSTATE\|', fragment, maxsplit=1)[0]
    return fragment

def postback(session, viewstate, viewstate_generator, event_target, event_source, fields):
    body = {
        "ctl00$ctl12": f"ctl00$upnl|{event_source}",
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstate_generator,
        "__ASYNCPOST": "true",
    }
    body.update(fields)
    response = session.post(
        RGUHS_URL,
        data=body,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": RGUHS_URL,
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.text

def text_by_id(soup, element_id):
    node = soup.find(id=element_id)
    return node.get_text(" ", strip=True) if node else ""

def parse_result(fragment: str):
    soup = BeautifulSoup(fragment, "html.parser")
    name = text_by_id(soup, "ContentPlaceHolder1_lblStudentName")
    reg = text_by_id(soup, "ContentPlaceHolder1_lblRegisNo")
    exam = text_by_id(soup, "ContentPlaceHolder1_lblSemester")
    # If the page didn't render a result, report that clearly rather than returning a blank success.
    if not reg or not soup.find(id="ContentPlaceHolder1_GridPG"):
        alert = soup.get_text(" ", strip=True)
        if "not found" in alert.lower() or "invalid" in alert.lower():
            raise ValueError("RGUHS did not return a marksheet for this registration number and examination.")
        raise ValueError("The portal did not return a marksheet. Check the registration number and try again later.")

    table = soup.find(id="ContentPlaceHolder1_GridPG")
    rows = table.find_all("tr") if table else []
    headers = [th.get_text(" ", strip=True) for th in rows[0].find_all(["th", "td"])] if rows else []
    subjects = []
    for row in rows[1:]:
        cells = [td.get_text(" ", strip=True).replace("\xa0", "").strip() for td in row.find_all("td")]
        if not cells or len(cells) < 3 or not cells[0]:
            continue
        item = {}
        for idx, header in enumerate(headers):
            if idx < len(cells):
                item[header] = cells[idx]
        subjects.append(item)

    result = {
        "student_name": name,
        "registration_number": reg,
        "examination": exam,
        "mother_name": text_by_id(soup, "ContentPlaceHolder1_lblMother"),
        "father_name": text_by_id(soup, "ContentPlaceHolder1_lblFather"),
        "college": text_by_id(soup, "ContentPlaceHolder1_lblcollegeName"),
        "published_date": text_by_id(soup, "ContentPlaceHolder1_lblresultpublishdate"),
        "grand_total": text_by_id(soup, "ContentPlaceHolder1_lblSecMArks"),
        "marks_in_words": text_by_id(soup, "ContentPlaceHolder1_lblMarksofword"),
        "maximum_marks": text_by_id(soup, "ContentPlaceHolder1_lblMaxMarks"),
        "overall_result": text_by_id(soup, "ContentPlaceHolder1_lblResult"),
        "min_pass": text_by_id(soup, "ContentPlaceHolder1_lblMin"),
        "printed_date": text_by_id(soup, "ContentPlaceHolder1_lblprintdate"),
        "subjects": subjects,
    }
    if not result["overall_result"] and not subjects:
        raise ValueError("The portal returned a page, but no marks were found.")
    return result

@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/result")
def lookup_result(payload: LookupRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(client_ip)

    reg = payload.registration_number.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,20}", reg):
        raise HTTPException(status_code=400, detail="Enter a valid registration number using letters and numbers only.")
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Please confirm you have permission to check this result.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    try:
        page = session.get(RGUHS_URL, timeout=HTTP_TIMEOUT)
        page.raise_for_status()
        html = page.text
        viewstate = get_viewstate_from_html(html)
        vg_match = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"', html)
        if not vg_match:
            raise RuntimeError("Could not read the current page state.")
        viewstate_generator = vg_match.group(1)

        # Step 1: registration number triggers exam list.
        delta1 = postback(
            session, viewstate, viewstate_generator,
            "ctl00$ContentPlaceHolder1$txtReg",
            "ctl00$ContentPlaceHolder1$txtReg",
            {"ctl00$ContentPlaceHolder1$txtReg": reg},
        )
        viewstate = get_viewstate_from_delta(delta1)
        fragment1 = extract_update_panel(delta1)
        if "MBBS7A" not in fragment1 and "ddlExam" not in fragment1:
            raise ValueError("RGUHS did not return an examination option for this registration number.")

        # Step 2: select the returned MBBS Phase-I option, as shown by the live portal flow.
        delta2 = postback(
            session, viewstate, viewstate_generator,
            "ctl00$ContentPlaceHolder1$ddlExam",
            "ctl00$ContentPlaceHolder1$ddlExam",
            {
                "ctl00$ContentPlaceHolder1$txtReg": reg,
                "ctl00$ContentPlaceHolder1$ddlExam": "MBBS7A",
            },
        )
        viewstate = get_viewstate_from_delta(delta2)

        # Step 3: click View.
        delta3 = postback(
            session, viewstate, viewstate_generator,
            "",
            "ctl00$ContentPlaceHolder1$btnview",
            {
                "ctl00$ContentPlaceHolder1$txtReg": reg,
                "ctl00$ContentPlaceHolder1$ddlExam": "MBBS7A",
                "ctl00$ContentPlaceHolder1$btnview": "View",
            },
        )
        fragment3 = extract_update_panel(delta3)
        result = parse_result(fragment3)
        if result["registration_number"].upper() != reg:
            raise ValueError("The portal returned a different registration number. Please retry.")
        response = JSONResponse(result)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except HTTPException:
        raise
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 503:
            detail = "The official RGUHS result portal is temporarily unavailable (503). Please try again later."
        else:
            detail = f"The official RGUHS portal returned HTTP {status}. Please try again later."
        raise HTTPException(status_code=502, detail=detail)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not reach the official RGUHS portal. Please try again later.")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        # Avoid returning upstream HTML or internal details to the public.
        raise HTTPException(status_code=502, detail="The official portal response could not be processed. Please try again later.")
    finally:
        session.close()
