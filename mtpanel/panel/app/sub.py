"""Public links page (Persian, RTL) and QR rendering."""
import html
import io

import qrcode
import qrcode.image.svg


def render_qr_svg(text: str) -> bytes:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


TEXT = {
    "fa": {"dir": "rtl", "connect": "اتصال", "copy": "کپی لینک", "copied": "کپی شد ✓", "copy_prompt": "کپی کنید:",
           "empty": "فعلاً لینکی موجود نیست.",
           "lead": "لینک‌ها همیشه به‌روز هستند؛ اگر یکی وصل نشد، بقیه را امتحان کنید."},
    "en": {"dir": "ltr", "connect": "Connect", "copy": "Copy link", "copied": "Copied ✓", "copy_prompt": "Copy:",
           "empty": "No links available right now.",
           "lead": "These links are always up to date; if one does not connect, try another."},
}


def render_public(title, groups, token, lang="fa"):
    T = TEXT.get(lang, TEXT["fa"])
    e = html.escape
    parts = []
    for g in groups:
        cards = []
        for it in g["items"]:
            cards.append(f"""
          <div class="card">
            <div class="name" dir="ltr">{e(it['name'])}</div>
            <img class="qr" src="{e(token)}/qr/{it['uid']}/{g['nid']}.svg" alt="QR" loading="lazy">
            <div class="btns">
              <a class="btn primary" href="{e(it['tg'])}">{T['connect']}</a>
              <button class="btn" data-copy="{e(it['https'])}">{T['copy']}</button>
            </div>
          </div>""")
        parts.append(f'<h2>{e(g["node"])}</h2><div class="grid">{"".join(cards)}</div>')
    if not parts:
        parts.append(f'<div class="card empty">{T["empty"]}</div>')
    return f"""<!doctype html>
<html lang="{lang if lang in TEXT else 'fa'}" dir="{T['dir']}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{e(title)}</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mut:#6b7385;--line:#e4e7ee;--acc:#2a7de1}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#181c25;--fg:#e8ebf2;--mut:#98a0b3;--line:#262c39;--acc:#4a95ef}}}}
@font-face{{font-family:Vazirmatn;src:url(/static-font/Vazirmatn.woff2) format('woff2');font-display:swap}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font-family:Vazirmatn,Tahoma,sans-serif;line-height:1.7}}
.wrap{{max-width:900px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:20px;margin:4px 0 4px}} .sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
h2{{font-size:16px;margin:22px 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;text-align:center}}
.name{{font-weight:700;margin-bottom:6px}} .qr{{width:180px;max-width:100%;background:#fff;border-radius:10px;padding:5px}}
.btns{{display:flex;gap:8px;margin-top:10px}} .btn{{flex:1;padding:10px;border-radius:10px;border:1px solid var(--line);background:transparent;color:var(--fg);font:inherit;text-decoration:none;cursor:pointer}}
.btn.primary{{background:var(--acc);border-color:var(--acc);color:#fff}} .empty{{color:var(--mut)}}
</style></head><body><div class="wrap">
<h1>{e(title)}</h1><div class="sub">{T['lead']}</div>
{''.join(parts)}
</div>
<script>
document.querySelectorAll('[data-copy]').forEach(function(b){{b.onclick=function(){{
var v=b.dataset.copy;(navigator.clipboard?navigator.clipboard.writeText(v):Promise.reject()).then(function(){{b.textContent={T['copied']!r}}},function(){{prompt({T['copy_prompt']!r},v)}});}}}});
</script></body></html>"""
