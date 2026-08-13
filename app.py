#!/usr/bin/env python3
"""
The Day Archive - Label Maker
=============================
A single-purpose Streamlit app that turns Shopify orders into print-ready
57 x 32 mm address labels for a Munbyn (or any direct-thermal) label printer.

Inputs (three ways to choose orders):
  1. Screenshots  - drop images of the order list; OCR reads the order numbers.
  2. Paste numbers - type/paste order numbers (e.g. 2597, 2604).
  3. All unfulfilled - pull every unfulfilled order straight from Shopify.

Output: a PDF with ONE label per page, sized exactly to 57 x 32 mm, carrying
the brand logo, order number, recipient shipping address and a return address.
Print at 100% / Actual size on the label roll.

This app is completely independent of the main dashboard. It only needs a
Shopify Admin API access token (a "custom app" token) with read_orders scope.
"""

import io
import re
import time
import os

import requests
import streamlit as st
from PIL import Image, ImageOps, ImageStat

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# Optional OCR - only needed for the screenshot path. Imported lazily so the
# app still runs (paste / fetch modes) if tesseract isn't present.
try:
    import pytesseract
    _OCR_IMPORT_OK = True
except Exception:
    _OCR_IMPORT_OK = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LABEL_W = 57 * mm
LABEL_H = 32 * mm
PAD = 2.2 * mm
BRAND_RGB = (0.227, 0.204, 0.157)  # #3a3428
API_VERSION = "2024-01"
DEFAULT_RETURN = "From: The Day Archive · 393-395 Liverpool Rd, Strathfield NSW 2135"

# province name -> code fallback (Shopify usually gives province_code directly)
AU_STATE = {
    "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
    "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
    "northern territory": "NT", "australian capital territory": "ACT",
}

st.set_page_config(page_title="The Day Archive — Label Maker", page_icon="\U0001F4EE", layout="centered")


# ---------------------------------------------------------------------------
# Secrets helpers
# ---------------------------------------------------------------------------
def sget(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def clean_store(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"^https?://", "", url)
    return url.strip("/")


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------
def _headers(token):
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


ORDER_FIELDS = "id,name,order_number,shipping_address,created_at,fulfillment_status"


def test_connection(store, token):
    """Return (ok, message)."""
    store = clean_store(store)
    if not store or not token:
        return False, "Enter both a store URL and an access token."
    try:
        r = requests.get(
            f"https://{store}/admin/api/{API_VERSION}/shop.json",
            headers=_headers(token), timeout=20,
        )
        if r.status_code == 200:
            name = r.json().get("shop", {}).get("name", "your store")
            return True, f"Connected to {name}."
        if r.status_code == 401:
            return False, "401 Unauthorized - check the access token."
        if r.status_code == 404:
            return False, "404 - check the store URL (should be *.myshopify.com)."
        return False, f"Shopify returned {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Connection error: {e}"


def fetch_unfulfilled(store, token):
    """Fetch ALL unfulfilled orders (cursor pagination)."""
    store = clean_store(store)
    base = f"https://{store}/admin/api/{API_VERSION}/orders.json"
    out, page_info = [], None
    while True:
        if page_info:
            params = {"limit": 250, "page_info": page_info}  # cursor rules: only limit + page_info
        else:
            params = {
                "status": "any",
                "fulfillment_status": "unfulfilled",
                "limit": 250,
                "fields": ORDER_FIELDS,
            }
        r = requests.get(base, headers=_headers(token), params=params, timeout=30)
        if r.status_code != 200:
            st.error(f"Shopify API error {r.status_code}: {r.text[:200]}")
            break
        out.extend(r.json().get("orders", []))
        link = r.headers.get("Link", "")
        m = re.search(r'<[^>]*[?&]page_info=([^&>]+)[^>]*>;\s*rel="next"', link)
        if m:
            page_info = m.group(1)
        else:
            break
    return out


def fetch_one_by_name(store, token, number):
    """Look up a single order by its number (with or without #)."""
    store = clean_store(store)
    num = re.sub(r"\D", "", str(number))
    if not num:
        return None
    base = f"https://{store}/admin/api/{API_VERSION}/orders.json"
    params = {"status": "any", "name": num, "fields": ORDER_FIELDS, "limit": 5}
    for attempt in range(4):
        r = requests.get(base, headers=_headers(token), params=params, timeout=30)
        if r.status_code == 429:  # rate limited
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        orders = r.json().get("orders", [])
        # name filter can be fuzzy; prefer an exact digit match
        for o in orders:
            if re.sub(r"\D", "", o.get("name", "")) == num:
                return o
        return orders[0] if orders else None
    return None


# ---------------------------------------------------------------------------
# Transform order -> label data
# ---------------------------------------------------------------------------
def transform(o):
    sa = o.get("shipping_address") or {}
    name = sa.get("name") or " ".join(
        p for p in [sa.get("first_name"), sa.get("last_name")] if p
    )
    lines = []
    if sa.get("company"):
        lines.append(sa["company"])
    if sa.get("address1"):
        lines.append(sa["address1"])
    if sa.get("address2"):
        lines.append(sa["address2"])
    state = sa.get("province_code") or AU_STATE.get((sa.get("province") or "").lower(), sa.get("province") or "")
    city_line = " ".join(p for p in [sa.get("city"), state, sa.get("zip")] if p).strip()
    if city_line:
        lines.append(city_line)
    country = sa.get("country") or ""
    if country and country.lower() != "australia":
        lines.append(country)
    return {
        "order_no": o.get("name") or ("#" + str(o.get("order_number", ""))),
        "name": name or "(no name on order)",
        "addr_lines": lines,
        "has_address": bool(sa.get("address1")),
    }


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def extract_numbers(pil_img):
    """Return (list_of_numbers, raw_text). Handles light-on-dark screenshots."""
    img = pil_img.convert("L")
    if ImageStat.Stat(img).mean[0] < 115:          # dark background -> invert
        img = ImageOps.invert(img)
    img = ImageOps.autocontrast(img)
    if img.width < 1500:                            # upscale small text
        f = 1600 / img.width
        img = img.resize((int(img.width * f), int(img.height * f)))
    raw = pytesseract.image_to_string(img, config="--psm 6")
    nums = re.findall(r"#\s*(\d{3,6})", raw)        # prefer #-prefixed
    if not nums:
        nums = re.findall(r"\b(\d{4})\b", raw)      # fallback: 4-digit tokens
    return dedupe(nums), raw


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def parse_numbers(text):
    return dedupe(re.findall(r"\d{3,6}", text or ""))


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _wrap(c, text, font, size, max_w):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if c.stringWidth(t, font, size) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_label(c, o, return_text, logo):
    x0 = PAD
    top = LABEL_H - PAD
    max_w = LABEL_W - 2 * PAD

    # --- header: logo (left) + order number (right) ---
    logo_h = 5 * mm
    if logo is not None:
        try:
            iw, ih = logo.getSize()
            lw = min(logo_h * (iw / ih), 30 * mm)
            c.drawImage(logo, x0, top - logo_h, width=lw, height=logo_h,
                        preserveAspectRatio=True, anchor="nw", mask="auto")
        except Exception:
            pass
    c.setFont("Helvetica-Bold", 8)
    c.setFillColorRGB(*BRAND_RGB)
    c.drawRightString(LABEL_W - PAD, top - 3.6 * mm, o.get("order_no", ""))

    # divider
    yline = top - logo_h - 0.8 * mm
    c.setStrokeColorRGB(0.85, 0.84, 0.80)
    c.setLineWidth(0.4)
    c.line(x0, yline, LABEL_W - PAD, yline)

    # --- recipient ---
    c.setFillColorRGB(0, 0, 0)
    y = yline - 1.0 * mm
    for ln in _wrap(c, o.get("name", ""), "Helvetica-Bold", 10, max_w):
        y -= 3.5 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x0, y, ln)
    for line in o.get("addr_lines", []):
        for ln in _wrap(c, line, "Helvetica", 9, max_w):
            y -= 3.1 * mm
            c.setFont("Helvetica", 9)
            c.drawString(x0, y, ln)

    # --- return address, wrapped, pinned to bottom ---
    c.setFillColorRGB(0.33, 0.33, 0.33)
    rlines = _wrap(c, return_text, "Helvetica", 6.3, max_w)[:2]
    ry = PAD + 0.2 * mm + (len(rlines) - 1) * 2.4 * mm
    for ln in rlines:
        c.setFont("Helvetica", 6.3)
        c.drawString(x0, ry, ln)
        ry -= 2.4 * mm
    c.setFillColorRGB(0, 0, 0)


def _load_logo_image():
    """Load the brand wordmark from logo.png (this folder or repo root), or
    fall back to an embedded base64 copy (logo_b64.txt) so the app stays
    self-contained even if lifted into a fresh repo."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "logo.png"), os.path.join(here, "..", "logo.png")):
        if os.path.exists(p):
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                pass
    b64 = os.path.join(here, "logo_b64.txt")
    if os.path.exists(b64):
        try:
            import base64
            raw = base64.b64decode(open(b64).read())
            return Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception:
            pass
    return None


def _prep_logo(im):
    """Repaint the (white, transparent) wordmark in dark brand ink so it prints
    on a white thermal label. Thermal printers are monochrome, so dark = crisp
    black output. Returns an ImageReader or None."""
    if im is None:
        return None
    _, _, _, alpha = im.split()
    mask = alpha if alpha.getextrema()[0] < 250 else ImageOps.invert(im.convert("L"))
    ink = Image.new("RGBA", im.size, (58, 52, 40, 255))   # #3a3428
    out = Image.composite(ink, Image.new("RGBA", im.size, (0, 0, 0, 0)), mask)
    b = io.BytesIO()
    out.save(b, format="PNG")
    b.seek(0)
    return ImageReader(b)


def build_pdf(orders, return_text, logo_path=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_W, LABEL_H))
    logo = _prep_logo(_load_logo_image())
    for o in orders:
        _draw_label(c, o, return_text, logo)
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("\U0001F4EE The Day Archive — Label Maker")
st.caption("Munbyn 57×32mm · one label per order · no ink, no waste")

with st.sidebar:
    st.header("Shopify connection")
    store = st.text_input("Store URL", value=sget("SHOPIFY_STORE"),
                          placeholder="the-day-archive.myshopify.com")
    token = st.text_input("Admin API access token", value=sget("SHOPIFY_TOKEN"),
                          type="password", placeholder="shpat_...")
    if st.button("Test connection", use_container_width=True):
        ok, msg = test_connection(store, token)
        (st.success if ok else st.error)(msg)

    st.divider()
    st.header("Return address")
    return_text = st.text_area("Printed as the sender", value=sget("RETURN_ADDRESS", DEFAULT_RETURN), height=90)

    with st.expander("Setup help"):
        st.markdown(
            "**Get a Shopify token:** Shopify admin → Settings → Apps and "
            "sales channels → *Develop apps* → Create an app → grant "
            "`read_orders` → Install → copy the **Admin API access token** "
            "(`shpat_...`).\n\nPaste the store URL + token above, or store them as "
            "app **secrets** (`SHOPIFY_STORE`, `SHOPIFY_TOKEN`, `RETURN_ADDRESS`)."
        )

# session state
st.session_state.setdefault("orders", [])
st.session_state.setdefault("not_found", [])


def do_fetch(numbers):
    if not (clean_store(store) and token):
        st.error("Add your Shopify store URL + token in the sidebar first.")
        return
    numbers = dedupe(numbers)
    if not numbers:
        st.warning("No order numbers to fetch.")
        return
    found, missing = [], []
    prog = st.progress(0.0, text="Fetching from Shopify…")
    for i, n in enumerate(numbers):
        o = fetch_one_by_name(store, token, n)
        if o:
            found.append(transform(o))
        else:
            missing.append(n)
        prog.progress((i + 1) / len(numbers), text=f"Fetched {i+1}/{len(numbers)}")
        time.sleep(0.2)  # be kind to the rate limit
    prog.empty()
    st.session_state.orders = found
    st.session_state.not_found = missing


tab_ss, tab_paste, tab_all = st.tabs(["\U0001F4F8 From screenshots", "\U0001F522 Paste numbers", "\U0001F4CB All unfulfilled"])

with tab_ss:
    if not _OCR_IMPORT_OK:
        st.warning("OCR library not available in this environment. Use the other tabs, "
                   "or deploy with `packages.txt` containing `tesseract-ocr`.")
    st.write("Drop one or more screenshots of your order list. I'll read the order numbers.")
    files = st.file_uploader("Order screenshots", type=["png", "jpg", "jpeg"],
                             accept_multiple_files=True)
    detected = []
    if files and _OCR_IMPORT_OK:
        for f in files:
            try:
                nums, raw = extract_numbers(Image.open(f))
            except Exception as e:
                st.error(f"Could not read {f.name}: {e}")
                continue
            detected += nums
            with st.expander(f"{f.name} — {len(nums)} numbers found"):
                st.text(raw or "(no text)")
        detected = dedupe(detected)
    edited = st.text_area("Detected order numbers — review & fix before fetching",
                          value=", ".join(detected), key="ss_edit",
                          placeholder="2542, 2597, 2604 …")
    if st.button("Fetch these orders", key="ss_fetch", type="primary"):
        do_fetch(parse_numbers(edited))

with tab_paste:
    txt = st.text_area("Order numbers (any separator)", placeholder="2597, 2604, 2605", key="paste_txt")
    if st.button("Fetch these orders", key="paste_fetch", type="primary"):
        do_fetch(parse_numbers(txt))

with tab_all:
    st.write("Pull every unfulfilled order straight from Shopify — no screenshots needed.")
    if st.button("Fetch all unfulfilled", key="all_fetch"):
        if not (clean_store(store) and token):
            st.error("Add your Shopify store URL + token in the sidebar first.")
        else:
            with st.spinner("Fetching unfulfilled orders…"):
                st.session_state.raw_unfulfilled = fetch_unfulfilled(store, token)
    raw = st.session_state.get("raw_unfulfilled")
    if raw is not None:
        opts = [o.get("name", "") for o in raw]
        st.caption(f"{len(opts)} unfulfilled orders found.")
        chosen = st.multiselect("Select orders to label", opts, default=opts, key="all_sel")
        if st.button("Use selected", key="all_use", type="primary"):
            picked = [transform(o) for o in raw if o.get("name") in set(chosen)]
            st.session_state.orders = picked
            st.session_state.not_found = []

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
orders = st.session_state.orders
missing = st.session_state.not_found
if missing:
    st.warning("Not found in Shopify: " + ", ".join(missing))

if orders:
    st.divider()
    st.subheader(f"✅ {len(orders)} label(s) ready")
    st.dataframe(
        [{"Order": o["order_no"], "Name": o["name"],
          "Address": " / ".join(o["addr_lines"]) or "⚠️ no shipping address"}
         for o in orders],
        use_container_width=True, hide_index=True,
    )
    no_addr = [o["order_no"] for o in orders if not o["has_address"]]
    if no_addr:
        st.warning("These orders have no shipping address (will print name only): " + ", ".join(no_addr))

    pdf = build_pdf(orders, return_text)
    st.download_button(
        "⬇️  Download labels PDF (57×32mm)", data=pdf,
        file_name="day-archive-labels.pdf", mime="application/pdf",
        type="primary", use_container_width=True,
    )
    st.caption("Print at **100% / Actual size**, paper = 57×32mm, margins = none. "
               "One label per page feeds one sticker on the Munbyn.")
else:
    st.info("Choose orders using a tab above, then a **Download labels PDF** button appears here.")
