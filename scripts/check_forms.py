"""Real-browser form regressions on local HTML. No model calls or external websites."""

import json
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = '''<!doctype html><title>Form reliability fixture</title>
<style>body{margin:20px}input{display:block;width:250px;margin:8px} [role=option]{padding:8px}
#unrelated{position:absolute;top:3000px}</style>
<fieldset><legend>Route</legend><form>
<div><label for="origin">Origin</label><input id="origin" name="Departure" placeholder="City"></div>
<label for="broken-id">Destination</label>
<div><input id="destination" name="Arrival" placeholder="City" role="combobox"
 aria-controls="choices" aria-expanded="false" aria-describedby="hint"></div>
<p id="hint">Choose a city</p><div id="choices" role="listbox"></div>
<input id="password" type="password" value="secret-must-not-leak">
<input id="hidden" type="hidden" value="secret-must-not-leak">
</form></fieldset><p id="unrelated">Unrelated</p>
<script>
window.inputs=0;
var field=document.getElementById('destination'), list=document.getElementById('choices');
function install(mode) {
 field.oninput=()=>{
  window.inputs++;
  field.setAttribute('aria-expanded','true');
  if (mode==='reject') {field.value=''; return;}
  if (mode==='replace') {setTimeout(()=>{field.outerHTML=field.outerHTML},50);return;}
  if (mode==='missing') return;
  setTimeout(()=>{
   list.innerHTML='<div role="option">Berlin, Germany</div>';
   list.firstChild.onclick=()=>{field.value='Berlin, Germany';list.hidden=true;
    field.setAttribute('aria-expanded','false')};
  },600);
 };
}
</script>'''


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        def reset(mode):
            browser.evaluate("document.open();document.write(" + json.dumps(HTML) + ");document.close()")
            browser.evaluate("scrollTo({top:0,behavior:'instant'});install(" + json.dumps(mode) + ")")
            p = browser.observe(screenshot=False)
            return p, next(a for a in p["actions"] if a["kind"] == "fill" and
                           a.get("context", {}).get("name") == "Arrival")

        page, field = reset("delayed")
        assert field["context"]["name"] == "Arrival"
        assert field["context"]["labels"] == []  # Broken HTML label must not become an explicit association.
        assert field["context"]["group"] == "Route"
        assert field["context"]["description"] == "Choose a city"
        def secret_paths(value, path="page"):
            if isinstance(value, dict):
                return [p for k, v in value.items() for p in secret_paths(v, f"{path}.{k}")]
            if isinstance(value, list):
                return [p for i, v in enumerate(value) for p in secret_paths(v, f"{path}[{i}]")]
            return [path] if isinstance(value, str) and "secret-must-not-leak" in value else []

        # The local data: URL itself contains this fixture's HTML, including synthetic secrets.
        assert set(secret_paths(page)) <= {"page.url", "page.marker[1]", "page.page_key[1]"}, secret_paths(page)
        passed.append("bounded observed context preserves broken-label and safe field evidence")

        browser.evaluate("document.querySelector('#destination').name='Changed'")
        assert not browser.fresh(page)
        passed.append("field-meaning change invalidates freshness")

        page, field = reset("delayed")
        browser.evaluate("list.innerHTML='<div role=option>Old origin suggestion</div>'")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a.get("node") == field["node"] and a["kind"] == "fill")
        browser.act(field, page, text="Berlin")
        page = browser.observe(screenshot=False)
        effect = page["after_action"]
        assert effect["status"] == "confirmed", effect
        assert 500 <= effect["wait_ms"] <= 3000, effect
        assert browser.evaluate("window.inputs") == 1
        option = next(a for a in page["actions"] if a.get("role") == "option")
        assert option["owner_node"] == field["node"]
        browser.act(option, page)
        page = browser.observe(screenshot=False)
        assert page["after_action"]["reason"] == "suggestion_accepted", page["after_action"]
        assert browser.evaluate("field.value") == "Berlin, Germany"
        passed.append("waits for delayed owned suggestions and independently checks selection")

        page, field = reset("delayed")
        browser.act(field, page, text="Berlin")
        page = browser.observe(screenshot=False)
        option = next(a for a in page["actions"] if a.get("role") == "option")
        browser.evaluate("field.removeAttribute('aria-controls');"
                         "document.querySelector('#origin').setAttribute('role','combobox');"
                         "document.querySelector('#origin').setAttribute('aria-controls','choices')")
        assert not browser.fresh(page, option)
        try:
            browser.act(option, page)
        except StalePage:
            pass
        else:
            raise AssertionError("Suggestion moved to a different field but was still clicked")
        passed.append("changing suggestion ownership invalidates its execution guard")

        page, field = reset("delayed")
        browser.act(field, page, text="Berlin")
        page = browser.observe(screenshot=False)
        # Closing a popup without updating/accepting the query is not a successful selection.
        browser.evaluate("list.firstChild.onclick=()=>{list.hidden=true;field.setAttribute('aria-expanded','false')}")
        page = browser.observe(screenshot=False)
        option = next(a for a in page["actions"] if a.get("role") == "option")
        browser.act(option, page)
        page = browser.observe(screenshot=False)
        assert page["after_action"]["reason"] == "selection_unconfirmed", page["after_action"]
        passed.append("closing suggestions without accepting a value does not confirm selection")

        for mode, expected in [("reject", "input_rejected"), ("missing", "autocomplete_timeout"),
                               ("replace", "field_replaced_or_document_changed")]:
            page, field = reset(mode)
            browser.act(field, page, text="Berlin")
            page = browser.observe(screenshot=False)
            assert page["after_action"]["reason"] == expected, page["after_action"]
            assert browser.evaluate("window.inputs") == 1
            passed.append(mode + " reports its actual result without repeating input")

        page, field = reset("missing")
        browser.evaluate("document.querySelector('#origin').setAttribute('role','combobox');"
                         "document.querySelector('#origin').setAttribute('aria-expanded','true');"
                         "field.removeAttribute('aria-controls');field.setAttribute('aria-expanded','true');"
                         "field.focus();list.innerHTML='<div role=option>Ambiguous</div>'")
        page = browser.observe(screenshot=False)
        option = next(a for a in page["actions"] if a.get("role") == "option")
        assert "owner_node" not in option
        passed.append("ambiguous list ownership is not asserted")

        page, field = reset("missing")
        browser.evaluate("field.style.position='absolute';field.style.top='2000px'")
        page = browser.observe(screenshot=False)
        hint = next(e for e in page["fields_outside_view"] if e["context"]["name"] == "Arrival")
        assert hint["position"] == "below"
        assert not any(a.get("node") == hint["node"] for a in page["actions"])
        down = next(a for a in page["actions"] if a["id"] == "scroll_down")
        browser.act(down, page)
        page = browser.observe(screenshot=False)
        assert page["after_action"]["reason"] == "scroll_moved", page["after_action"]
        assert page["scroll"]["y"] > 0
        passed.append("offscreen fields are hints only; scroll movement is observed before continuing")

        reset("missing")
        browser.evaluate("const cover=document.createElement('div');"
                         "cover.style.cssText='position:fixed;inset:0;z-index:99999;background:white';"
                         "document.body.append(cover)")
        page = browser.observe(screenshot=False)
        assert not any(a["kind"] == "fill" for a in page["actions"])
        passed.append("covered fields are not offered for text generation or input")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} form checks; no model calls")


if __name__ == "__main__":
    main()
