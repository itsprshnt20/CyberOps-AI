
"""Exercise the complete UI in a clean demo database."""
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8501"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def select(page, label, text, kind="stSelectbox"):
    page.get_by_test_id(kind).filter(has_text=label).get_by_role("combobox").click()
    page.get_by_role("option", name=text, exact=False).click()
    if kind == "stMultiSelect":
        page.keyboard.press("Escape")
        expect(page.get_by_role("button", name="Attach selected alerts")).to_be_enabled()


def note(page, text, expected, question=False):
    if question:
        select(page, "Entry type", "Open question")
    page.get_by_role("textbox", name="Author", exact=True).fill("Demo Analyst")
    page.get_by_role("textbox", name="Note or question").fill(text)
    page.get_by_role("button", name="Add entry").click()
    expect(page.get_by_text(expected, exact=False)).to_be_visible()
    expect(page.get_by_role("textbox", name="Note or question")).to_have_value("")


def save(page):
    page.get_by_role("textbox", name="Next owner").fill("Next Shift")
    page.get_by_role("textbox", name="Next action").fill("Verify the alert records in their source systems.")
    page.get_by_role("button", name="Review handoff").click()
    expect(page.get_by_role("heading", name="Review draft briefing")).to_be_visible()
    page.get_by_role("button", name="Save reviewed handoff").click()
    expect(page.get_by_role("heading", name="Review draft briefing")).to_have_count(0)


def download(page):
    expect(page.get_by_text("Running...", exact=True)).to_be_hidden()
    with page.expect_download() as result:
        page.get_by_role("button", name="Download handoff Markdown").click()
    return result.value.path().read_bytes()


def image(page, selector, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        expect(page.get_by_text("Running...", exact=True)).to_be_hidden()
        page.locator(selector).screenshot(path=SHOTS / name, animations="disabled")


def main():
    expect.set_options(timeout=45000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(accept_downloads=True, viewport={"width": 1280, "height": 3000})
        page.goto(URL)
        expect(page.get_by_role("heading", name="CyberOps AI")).to_be_visible()
        page.get_by_role("button", name="Load identity demo").click()
        expect(page.get_by_text("New: 2 · Duplicate: 0", exact=False)).to_be_visible()
        upload = page.locator('input[type="file"]')
        expect(upload).to_have_count(1)
        upload.set_input_files(SAMPLES / "endpoint.json")
        expect(page.get_by_text("endpoint.json", exact=True)).to_be_visible()
        page.get_by_role("button", name="Import JSON").click()
        expect(page.get_by_text("New: 2 · Duplicate: 0", exact=False)).to_be_visible()
        page.get_by_role("button", name="Load identity demo").click()
        expect(page.get_by_text("New: 0 · Duplicate: 2", exact=False)).to_be_visible()
        page.get_by_role("textbox", name="Case title").fill("Synthetic shift review")
        page.get_by_role("button", name="Create case").click()
        expect(page.get_by_role("heading", name="Case timeline")).to_be_visible()
        select(page, "Alerts to attach", "Repeated sign-in failures", "stMultiSelect")
        page.get_by_role("button", name="Attach selected alerts").click()
        expect(page.get_by_text("Linked alert records", exact=True)).to_be_visible()
        select(page, "Alerts to attach", "Endpoint policy warning", "stMultiSelect")
        page.get_by_role("button", name="Attach selected alerts").click()
        expect(page.get_by_text("Running...", exact=True)).to_be_hidden()
        expect(page.get_by_text("Linked alerts: 2", exact=False)).to_be_visible()
        note(page, "Original analyst note.", "Analyst notes: 1")
        note(page, "Was the warning expected?", "Open questions: 1", question=True)
        image(page, ".st-key-case_panel", "case-timeline.png")
        page.get_by_text("Handoff", exact=True).click()
        save(page)
        first = download(page)
        assert b"Revision 1" in first and b"Case ID:" in first and first.count(b"Alert ID ") == 2
        assert b"Original analyst note." in first and b"Was the warning expected?" in first
        assert b"Routine sign-in review" not in first and b"Next Shift" in first

        page.get_by_text("Timeline and evidence", exact=True).click()
        select(page, "Alerts to attach", "Endpoint inventory check", "stMultiSelect")
        page.get_by_role("button", name="Attach selected alerts").click()
        expect(page.get_by_text("Running...", exact=True)).to_be_hidden()
        expect(page.get_by_text("Linked alerts: 3", exact=False)).to_be_visible()
        note(page, "Second shift observation.", "Analyst notes: 2")
        page.get_by_text("Handoff", exact=True).click()
        expect(page.get_by_text("Latest handoff predates", exact=False)).to_be_visible()
        save(page)
        expect(page.get_by_role("combobox", name="Selected Revision 2. Saved revision")).to_be_visible()
        image(page, ".st-key-handoff_panel", "analyst-handoff.png")
        second = download(page)
        assert b"Revision 2" in second and second.count(b"Alert ID ") == 3
        assert b"Second shift observation." in second and b"Endpoint inventory check" in second
        select(page, "Saved revision", "Revision 1")
        expect(page.get_by_text("This saved revision predates", exact=False)).to_be_visible()
        assert download(page) == first
        print("PASS: two sources, deduplication, linked alerts, notes, question, reviewed revisions, exact historical export")
        browser.close()


if __name__ == "__main__":
    main()
