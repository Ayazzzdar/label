# The Day Archive — Label Maker

A standalone Streamlit app that turns Shopify orders into print-ready
**57 × 32 mm** address labels for a **Munbyn** (or any direct-thermal) label
printer. One label per page, no ink, no waste.

It is completely independent of the main dashboard — it only reads orders from
Shopify and generates a PDF.

## Choose orders three ways
1. **📸 From screenshots** — drop images of your Shopify order list; OCR reads
   the order numbers (you review/fix them before fetching).
2. **🔢 Paste numbers** — type/paste order numbers like `2597, 2604`.
3. **📋 All unfulfilled** — pull every unfulfilled order directly from Shopify
   and tick the ones you want.

Then hit **Download labels PDF** and print at **100% / Actual size**.

## 1. Get a Shopify Admin API token (once, ~2 min)
Shopify admin → **Settings → Apps and sales channels → Develop apps** →
**Create an app** → **Configure Admin API scopes** → enable **`read_orders`**
→ **Install app** → copy the **Admin API access token** (`shpat_...`).

## 2. Run locally
```bash
pip install -r requirements.txt
# macOS: brew install tesseract   |   Ubuntu: sudo apt install tesseract-ocr
streamlit run app.py
```
Enter the store URL + token in the sidebar (or create `.streamlit/secrets.toml`
from the example file).

## 3. Deploy free (its own URL, separate from the dashboard)
**Streamlit Community Cloud** (recommended):
1. Push this repo to GitHub.
2. https://share.streamlit.io → **New app**.
3. Set **Main file path** to `app.py`.
4. **Advanced settings → Secrets** → paste the contents of
   `.streamlit/secrets.toml.example` with your real values.
5. Deploy. `packages.txt` auto-installs `tesseract-ocr` for the screenshot OCR.

**Hugging Face Spaces** also works — create a Streamlit Space, add the same
files, and set the secrets in the Space settings.

## Label spec
- 57 × 32 mm, one order per page, margins 0.
- Contents: brand logo + order number, recipient shipping address, return address.
- Swap `logo.png` to change the branding.
