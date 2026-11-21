#!/usr/bin/env python3
"""ヤマビヨリのレポート仕様(JSON)を、自己完結した1枚のHTMLにする。標準ライブラリのみ。

Claude Code でも Codex でも、エージェントは HTML を自分で書かず、このスクリプトに JSON を渡す。
見た目(CSS・骨組み)はこちらで固定するので、どのエージェントでも同じページになる。

使い方:
    python -X utf8 scripts/render_report.py out/report.json --out out/report.html
    python -X utf8 scripts/render_report.py out/report.json --out out/report.html --embed-images
    python -X utf8 scripts/render_report.py --data out/山.json --out out/山.html --panorama-html out/panorama/m058.html --panorama-viewpoint-html out/panorama/m058v.html  (載せる展望図は1枚。展望地の図があればそれ)

JSON の書き方は skills/yamabiyori/references/report_style.md を参照。
"""
import argparse
import base64
import html
import json
import mimetypes
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent
CSS_PATH = HERE.parent / "assets" / "report" / "report.css"

FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Shippori+Mincho:wght@500;600;700&"
    'family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap">'
)

TAB_JS = """(function(){
  document.querySelectorAll('[data-tabs]').forEach(function(root){
    var btns=root.querySelectorAll('.tab-btn'),panels=root.querySelectorAll('.tab-panel');
    btns.forEach(function(btn){btn.addEventListener('click',function(){
      var id=btn.getAttribute('data-tab');
      btns.forEach(function(b){b.classList.toggle('active',b===btn);b.setAttribute('aria-selected',b===btn?'true':'false');});
      panels.forEach(function(p){p.classList.toggle('active',p.getAttribute('data-panel')===id);});
    });});
  });
})();"""

MODES = ("diagnosis", "plan", "explore")
LEVELS = ("safe", "caution", "warning")


class SpecError(ValueError):
    pass


# モードごとの必須要素(ブロックの "id")。情報量がエージェントによってばらつかないための最低ライン。
# 取得できなかったものは、そのブロックに "omitted": "理由" を書けば省略できる(ページ上にも理由が出る)。
REQUIRED_IDS = {
    "diagnosis": ("hero", "synoptic", "scores"),
    "plan": ("daycards", "map", "synoptic", "scores"),
    "explore": ("synoptic", "regions", "ranking"),
}
ID_HINTS = {
    "hero": "今日のスコア・危険信号・統計(type: hero)",
    "daycards": "日別カード(type: daycards)",
    "map": "ルート天気マップ(type: map)",
    "synoptic": "気圧配置の節(type: section)。images(天気図・ひまわり)と analysis(気圧配置・点予報との整合・中期的な懸念)を含める",
    "scores": "日別/地点別スコア表(type: table を含む section)",
    "regions": "山域サマリー表",
    "ranking": "日別ランキング(type: tabs)",
}


def walk(blocks):
    """ブロックを入れ子の中まで順にたどる。"""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        yield b
        yield from walk(b.get("content"))
        for t in b.get("tabs") or []:
            yield from walk(t.get("content"))


def check_completeness(spec):
    """必須要素の抜けを洗い出す。問題があれば SpecError。"""
    blocks = {b["id"]: b for b in walk(spec.get("blocks")) if b.get("id")}
    problems = []
    for rid in REQUIRED_IDS[spec["mode"]]:
        b = blocks.get(rid)
        if b is None:
            problems.append(f'必須の要素 "{rid}" がありません → {ID_HINTS[rid]}。載せられない事情があるなら、同じ id のブロックに "omitted": "理由" を書く')
            continue
        if b.get("omitted"):
            continue
        if rid == "synoptic":
            inner = list(walk(b.get("content")))
            if not any(c.get("type") == "analysis" and c.get("items") for c in inner):
                problems.append('"synoptic" に analysis(気圧配置の解説)がありません。天気図を読めなかった場合も、数値から読み取れる範囲を tag 付きで書く')
            if not any(c.get("type") == "images" and c.get("items") for c in inner) and not b.get("images_omitted"):
                problems.append('"synoptic" に images(天気図・ひまわり)がありません。取得に失敗したなら "images_omitted": "理由" を書く(fetch_synoptic.py の failed の内容)')
    if problems:
        raise SpecError("レポートの必須要素が足りません:\n  - " + "\n  - ".join(problems))


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def rich(value):
    """エスケープしたうえで **太字** だけを許可する。"""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(value))


def css_class(value, allowed=None):
    """class 名は英数字・ハイフン・アンダースコアだけ通す。"""
    value = "" if value is None else str(value)
    if not re.fullmatch(r"[A-Za-z0-9_\- ]*", value):
        raise SpecError(f"不正な class 名: {value!r}")
    if allowed is not None and value and value not in allowed:
        raise SpecError(f"class は {allowed} のいずれか: {value!r}")
    return value


def score_level(score):
    """スコア→色の段階。従来ページと同じ閾値(80以上 safe、40以上 caution、未満 warning)。"""
    if score >= 80:
        return "safe"
    if score >= 40:
        return "caution"
    return "warning"


# ---------- 展望図の埋め込み ----------
# 展望図(mountain_weather_detail.py --panorama の HTML)は描き方に手を入れず、iframe の srcdoc に入れる。
# 埋め込み用の写しにだけ、head の終わりに下の CSS・JS を足す(展望図のファイルと panorama/ の出力は変えない)。
# 枠の高さは、中身(図と時刻・方角のボタン)の高さを postMessage で受けて合わせる。スクリプトが動かないときの高さ:
PANORAMA_IFRAME_HEIGHT = 460
PANORAMA_MSG = "yamabiyori-panorama"
# 展望図の、横向きの画面向けの CSS の塊。枠の中では枠の縦横で決まってしまうので、レポートの画面の縦横で決まるよう
# class(html.yb-land)の条件に書き換える(枠の高さを中身に合わせると、縦向きの画面でも枠が横長になるため)。
PANORAMA_LANDSCAPE_MEDIA = "@media (orientation:landscape) and (max-height:500px){"
# 展望図は図の大きさを window.innerHeight で決める。枠の中ではそれが枠の高さになり、枠を中身に合わせると
# 循環するので、レポートの画面の高さを返すようにする。図の下の説明(視点の状態・題・凡例など)は出さない。
PANORAMA_CHILD_JS = r"""(function(){
var K='%(msg)s',VH=0,de=document.documentElement,last=0;
try{VH=window.parent.innerHeight||0}catch(e){}
if(!VH)VH=(window.screen&&screen.height)||700;
try{Object.defineProperty(window,'innerHeight',{configurable:true,get:function(){return VH}})}catch(e){}
function land(){de.classList.toggle('yb-land',(window.innerWidth||0)>VH&&VH<=500)}
land();window.addEventListener('resize',land);
function post(){var b=document.body;if(!b)return;var h=Math.ceil(b.getBoundingClientRect().height);
 if(h>0&&h!==last){last=h;try{window.parent.postMessage({type:K+'-height',h:h},'*')}catch(e){}}}
window.addEventListener('message',function(e){var d=e.data;if(!d||d.type!==K+'-vh'||e.source!==window.parent)return;
 var v=+d.vh;if(v>0&&v!==VH){VH=v;window.dispatchEvent(new Event('resize'))}post()});
window.addEventListener('load',function(){post();if(window.ResizeObserver)new ResizeObserver(post).observe(document.body)});
})();"""
PANORAMA_CHILD_CSS = ("main.p39>#frame~*{display:none!important}main.p39{padding:0 0 1px!important}#frame{margin:0!important}"
                      "html,body{background:%(light)s!important}"
                      "@media (prefers-color-scheme: dark){html,body{background:%(dark)s!important}}")
PANORAMA_PARENT_JS = r"""(function(){
var K='%(msg)s',f=[].slice.call(document.querySelectorAll('iframe.panorama')),t;
function send(x){try{x.contentWindow.postMessage({type:K+'-vh',vh:window.innerHeight},'*')}catch(e){}}
f.forEach(function(x){x.addEventListener('load',function(){send(x)});send(x)});
window.addEventListener('message',function(e){var d=e.data;if(!d||d.type!==K+'-height')return;
 f.forEach(function(x){if(x.contentWindow===e.source){var h=Math.round(+d.h);if(h>0&&h<20000)x.style.height=h+'px'}})});
window.addEventListener('resize',function(){clearTimeout(t);t=setTimeout(function(){f.forEach(send)},120)});
})();""" % {"msg": PANORAMA_MSG}


def report_bg_colors(css_text):
    """report.css の地の色(--bg)。返り値: (ライト, ダーク)。"""
    found = re.findall(r"--bg:\s*(#[0-9A-Fa-f]{3,8})", css_text)
    if len(found) < 2:
        raise SpecError("report.css から地の色(--bg のライトとダーク)を読めません")
    return found[0], found[1]


def _landscape_to_class(page):
    """展望図の、横向きの画面向けの @media の塊を html.yb-land の条件に書き換える。塊が無ければそのまま。"""
    at = page.find(PANORAMA_LANDSCAPE_MEDIA)
    if at < 0:
        return page
    i, depth = at + len(PANORAMA_LANDSCAPE_MEDIA), 1
    j = i
    while j < len(page) and depth:
        depth += {"{": 1, "}": -1}.get(page[j], 0)
        j += 1
    inner = page[i:j - 1]
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", inner)
    out = "".join(",".join("html.yb-land " + sel.strip() for sel in sels.split(",")) + "{" + decl + "}"
                  for sels, decl in rules)
    return page[:at] + out + page[j:]


def panorama_embed_page(page, bg):
    """iframe に入れる展望図の写し(元の HTML の head の終わりに CSS・JS を足し、横向きの CSS を書き換えたもの)。"""
    add = (f"<style>{PANORAMA_CHILD_CSS % {'light': bg[0], 'dark': bg[1]}}</style>"
           f"<script>{PANORAMA_CHILD_JS % {'msg': PANORAMA_MSG}}</script>")
    page = _landscape_to_class(page)
    at = page.find("</head>")
    return page[:at] + add + page[at:] if at >= 0 else add + page


WEEKDAYS = "月火水木金土日"


def load_panorama(path, kind):
    """展望図の HTML を読み、見出しに使う視点の名前と予報の日を取り出す。kind: "viewpoint" か "summit"。"""
    page = Path(path).read_bytes().decode("utf-8")
    name = None
    if kind == "viewpoint":
        # 展望図を作るときの視点のデータ(viewpoint.source_text)が書く「視点: <名前>(OpenStreetMap の展望地。…)」
        m = re.search(r"視点: ([^<(]+?)\(OpenStreetMap の展望地", page)
        name = m.group(1).strip() if m and m.group(1).strip() != "名前のない展望地" else None
    m = re.search(r"<title>[^<]*からの展望と雲\((\d{4}-\d{2}-\d{2})\)</title>", page)
    day = None
    if m:
        import datetime
        d = datetime.date.fromisoformat(m.group(1))
        day = f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
    if kind == "summit":
        heading = "山頂からの展望"
    else:
        heading = f"{name}(展望地)からの展望" if name else "展望地からの展望"
    return {"kind": kind, "page": page, "name": name, "day": day, "heading": heading}


class Renderer:
    def __init__(self, spec, base_dir, embed_images, panorama=None):
        self.spec = spec
        # load_panorama() の返り値(1枚だけ)。None なら節を作らない(従来と同じ出力)
        self.panorama = panorama
        self.base_dir = Path(base_dir)
        self.embed = embed_images
        self.has_tabs = False
        self.warnings = []
        self._tab_seq = 0

    # ---------- 画像 ----------
    def img_src(self, src):
        if not self.embed:
            return esc(src)
        path = (self.base_dir / src).resolve()
        if not path.is_file():
            self.warnings.append(f"画像が見つかりません: {src}")
            return esc(src)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"

    # ---------- ブロック ----------
    def render_blocks(self, blocks):
        return "".join(self.render_block(b) for b in blocks)

    def render_block(self, b):
        kind = b.get("type")
        fn = getattr(self, f"block_{kind}", None)
        if fn is None:
            raise SpecError(f"未知のブロック type: {kind!r}")
        if b.get("omitted") and kind != "section":
            return f'<div class="ref-note">※ 省略しました: {rich(b["omitted"])}</div>'
        return fn(b)

    def block_section(self, b):
        head = ""
        if b.get("title") or b.get("note"):
            head = '<div class="section-head">'
            if b.get("title"):
                head += f"<h2>{rich(b['title'])}</h2>"
            if b.get("note"):
                head += f'<div class="note">{rich(b["note"])}</div>'
            head += "</div>"
        if b.get("omitted"):  # 節ごと省略 → 理由だけを表示する(情報が無いことを隠さない)
            return f'<div class="section">{head}<div class="ref-note">※ この節は省略しました: {rich(b["omitted"])}</div></div>'
        notice = ""
        if b.get("images_omitted"):
            notice = f'<div class="ref-note">※ 天気図・衛星画像は取得できませんでした: {rich(b["images_omitted"])}</div>'
        return f'<div class="section">{head}{notice}{self.render_blocks(b.get("content", []))}</div>'

    def block_hero(self, b):
        out = ['<div class="hero"><div class="hero-main">']
        out.append(f'<div class="hero-label">{rich(b.get("label", ""))}</div>')
        out.append(
            f'<div class="hero-top"><span class="n num">{esc(b.get("score", "-"))}</span>'
            f'<span class="l">/ {esc(b.get("max", 100))}</span></div>'
        )
        if b.get("hazard"):
            h = b["hazard"]
            level = css_class(h.get("class", "warning"), LEVELS)
            out.append(f'<div class="hero-hazard"><span class="badge {level}">{rich(h.get("text", ""))}</span></div>')
        if b.get("stats"):
            out.append('<div class="hero-stats">')
            for s in b["stats"]:
                out.append(f'<div class="stat"><div class="l">{esc(s.get("l"))}</div><div class="v num">{esc(s.get("v"))}</div></div>')
            out.append("</div>")
        out.append("</div>")
        if b.get("next"):
            out.append('<div class="nextdays">')
            for n in b["next"]:
                level = css_class(n.get("class", ""), LEVELS)
                out.append(
                    '<div class="nextcard"><div>'
                    f'<div class="d">{esc(n.get("d"))}</div><div class="h">{rich(n.get("h", ""))}</div></div>'
                    f'<div class="s num {level}">{esc(n.get("s"))}</div></div>'
                )
            out.append("</div>")
        out.append("</div>")
        return "".join(out)

    def block_daycards(self, b):
        out = ['<div class="daycards">']
        for c in b.get("cards", []):
            level = css_class(c.get("class", "safe"), LEVELS)
            out.append(
                f'<div class="daycard {level}">'
                f'<div class="dd">{esc(c.get("dd"))}</div><div class="dt">{esc(c.get("dt"))}</div>'
                f'<div class="route">{rich(c.get("route", ""))}</div>'
                f'<div class="verdict {level}">{rich(c.get("verdict", ""))}</div>'
                f'<div class="detail">{rich(c.get("detail", ""))}</div></div>'
            )
        out.append("</div>")
        return "".join(out)

    def block_alert(self, b):
        body = f"<p>{rich(b['body'])}</p>" if b.get("body") else ""
        return f'<div class="alert"><div class="h">⚠ {rich(b.get("heading", ""))}</div>{body}</div>'

    def block_note(self, b):
        return f'<div class="ref-note">{rich(b.get("text", ""))}</div>'

    def block_summary(self, b):
        return f'<p class="summary">{rich(b.get("text", ""))}</p>'

    def block_images(self, b):
        out = ['<div class="synoptic-grid">']
        for im in b.get("items", []):
            out.append(
                f'<div class="imgcard"><img src="{self.img_src(im["src"])}" alt="{esc(im.get("alt", ""))}">'
                f'<div class="cap">{esc(im.get("cap", ""))}</div></div>'
            )
        out.append("</div>")
        return "".join(out)

    def block_map(self, b):
        return (
            f'<div class="mapcard"><img src="{self.img_src(b["src"])}" alt="{esc(b.get("alt", ""))}">'
            f'<div class="cap">{esc(b.get("cap", ""))}</div></div>'
        )

    def block_analysis(self, b):
        out = ['<div class="analysis">']
        for p in b.get("items", []):
            tag = f'<span class="tag">{esc(p["tag"])}</span>' if p.get("tag") else ""
            out.append(f"<p>{tag}{rich(p.get('text', ''))}</p>")
        out.append("</div>")
        return "".join(out)

    def block_panels(self, b):
        out = ['<div class="twocol">']
        for p in b.get("panels", []):
            out.append("<div class=\"panel\">")
            if p.get("title"):
                out.append(f"<h3>{rich(p['title'])}</h3>")
            if p.get("note"):
                out.append(f'<div class="note">{rich(p["note"])}</div>')
            for r in p.get("rows", []):
                out.append(f'<div class="rowline"><span class="l">{esc(r.get("l"))}</span><span>{rich(r.get("v", ""))}</span></div>')
            if p.get("items"):
                out.append('<ul class="changelist">' + "".join(f"<li>{rich(i)}</li>" for i in p["items"]) + "</ul>")
            out.append("</div>")
        out.append("</div>")
        return "".join(out)

    def block_tabs(self, b):
        self.has_tabs = True
        self._tab_seq += 1
        pre = f"t{self._tab_seq}"
        tabs = b.get("tabs", [])
        nav = ['<div class="tabs-nav" role="tablist">']
        panels = []
        for i, t in enumerate(tabs):
            active = " active" if i == 0 else ""
            tid = f"{pre}-{i}"
            nav.append(
                f'<button class="tab-btn{active}" data-tab="{tid}" role="tab" aria-selected="{"true" if i == 0 else "false"}">'
                f'<span class="tab-date">{esc(t.get("label"))}</span><span class="tab-wk">{esc(t.get("wk", ""))}</span></button>'
            )
            head = f'<div class="panel-head"><h3>{rich(t["heading"])}</h3></div>' if t.get("heading") else ""
            panels.append(
                f'<div class="tab-panel{active}" data-panel="{tid}" role="tabpanel">{head}{self.render_blocks(t.get("content", []))}</div>'
            )
        nav.append("</div>")
        return f'<div data-tabs>{"".join(nav)}{"".join(panels)}</div>'

    def block_table(self, b):
        cols = b.get("columns", [])
        if not cols:
            raise SpecError("table には columns が必要")
        def th(c):
            w = c.get("width")  # 列の最小幅(px)。長い文字列の列が1文字ずつ折り返して行が縦に伸びるのを防ぐ
            style = f' style="min-width:{int(w)}px"' if w else ""
            return f'<th class="{"num" if c.get("kind") in ("num", "score") else ""}"{style}>{rich(c.get("label", ""))}</th>'
        head = "".join(th(c) for c in cols)
        groups = b.get("groups") or [{"rows": b.get("rows", [])}]
        body = []
        for g in groups:
            if g.get("label"):
                body.append(f'<tr class="grouprow"><td colspan="{len(cols)}">{rich(g["label"])}</td></tr>')
            for row in g.get("rows", []):
                body.append("<tr>" + "".join(self.cell(c, row.get(c["key"])) for c in cols) + "</tr>")
        min_w = f' style="min-width:{int(b["min_width"])}px"' if b.get("min_width") else ""
        return f'<div class="tablewrap"><table{min_w}><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'

    def cell(self, col, value):
        """セルの値は文字列・数値、または {"text":..., "class":..., "sub":..., "tag":...} の辞書。"""
        kind = col.get("kind", "text")
        extra_class = ""
        sub = tag = ""
        if isinstance(value, dict):
            extra_class = css_class(value.get("class", ""))
            sub = f'<span class="sub-elev">{esc(value["sub"])}</span>' if value.get("sub") else ""
            tag = f'<span class="tag2">{esc(value["tag"])}</span>' if value.get("tag") else ""
            text = value.get("text", "")
            numeric = value.get("score", text)
        else:
            text = value
            numeric = value
        if text is None:
            text = ""

        if kind == "score":
            try:
                n = float(numeric)
            except (TypeError, ValueError):
                return f'<td class="score num">{esc(text)}</td>'
            level = extra_class if extra_class in LEVELS else score_level(n)
            if isinstance(text, str) and text != "":
                shown = text  # 文字列で渡された場合はそのまま表示(例: "100"、"88.5")
            elif isinstance(numeric, float):
                shown = f"{n:.1f}"  # 小数で渡された場合は従来ページどおり小数1桁(81.0)
            else:
                shown = str(int(n))
            return f'<td class="score {level} num">{esc(shown)}</td>'
        if kind == "badge":
            if text in ("", "-", None):
                return '<td><span class="badge none">-</span></td>'
            level = css_class(extra_class or "warning")
            return f'<td><span class="badge {level}">{rich(text)}</span></td>'
        if kind == "conf":
            level = css_class(extra_class or "mid")
            return f'<td><span class="conf-pill {level}">{esc(text)}</span></td>'
        if kind == "confbar":
            try:
                n = max(0, min(100, float(numeric)))
            except (TypeError, ValueError):
                return f"<td>{esc(text)}</td>"
            return (
                f'<td><div class="confbar-wrap"><span class="v">{esc(text)}</span>'
                f'<span class="confbar"><i style="width:{n:g}%"></i></span></div></td>'
            )
        gold = '<span class="dot-gold" title="雲海条件"></span>' if isinstance(value, dict) and value.get("gold") else ""
        if kind == "num":
            return f'<td class="num {extra_class}">{rich(text)}{sub}{gold}</td>'
        classes = ["strong" if kind == "strong" else "", "muted" if kind == "muted" else "", "nowrap" if kind == "nowrap" else "", extra_class]
        cls = " ".join(c for c in classes if c)
        cls_attr = f' class="{cls}"' if cls else ""
        return f"<td{cls_attr}>{rich(text)}{sub}{tag}{gold}</td>"

    # ---------- 展望図 ----------
    def panorama_section(self, css_text):
        """展望の節(1枚だけ)。展望図の描き方は写さず、iframe の srcdoc に入れる(写しに埋め込み用の CSS・JS を足す)。"""
        p = self.panorama
        when = f"{p['day']}の予報にもとづく" if p.get("day") else "予報にもとづく"
        page = panorama_embed_page(p["page"], report_bg_colors(css_text))
        # 改行も文字参照にする(出力時の改行の変換で CR が増えたり、属性値の改行の正規化で中身が変わったりしないように)
        doc = esc(page).replace("\r", "&#13;").replace("\n", "&#10;")
        return (
            f'<div class="section" id="panorama"><div class="section-head"><h2>{esc(p["heading"])}</h2>'
            f'<div class="note">{when}展望図(目安)。時刻のボタンで雲が替わり、図を横になぞると方角が動きます。</div></div>'
            f'<iframe class="panorama" title="{esc(p["heading"])}の展望図" srcdoc="{doc}" '
            f'style="display:block;width:100%;height:{PANORAMA_IFRAME_HEIGHT}px;border:0;margin:0 0 16px"></iframe></div>'
        )

    @staticmethod
    def panorama_position(blocks):
        """展望の節を入れる位置(本文のブロックの番号)。総評のすぐあと。総評が無ければ、先頭の要約(無ければ先頭の見込み)のあと。"""
        for i, b in enumerate(blocks):
            if b.get("type") == "analysis" and any(it.get("tag") == "総評" for it in b.get("items") or []):
                return i + 1
        for kind in ("summary", "hero"):
            for i, b in enumerate(blocks):
                if b.get("type") == kind or (kind == "hero" and b.get("id") == "hero"):
                    return i + 1
        return 0

    # ---------- ページ ----------
    def render(self):
        s = self.spec
        mode = s.get("mode")
        if mode not in MODES:
            raise SpecError(f"mode は {MODES} のいずれか: {mode!r}")
        if not s.get("title"):
            raise SpecError("title が必要")
        check_completeness(s)
        sub = "".join(f"<span>{rich(x)}</span>" for x in s.get("sub", []))
        header = (
            "<header>"
            f'<div class="eyebrow">{rich(s.get("eyebrow", "ヤマビヨリ"))}</div>'
            f"<h1>{rich(s['title'])}</h1>"
            f'<div class="sub">{sub}</div></header>'
        )
        css = CSS_PATH.read_text(encoding="utf-8")
        blocks = s.get("blocks", [])
        if self.panorama:
            at = self.panorama_position(blocks)
            body = self.render_blocks(blocks[:at]) + self.panorama_section(css) + self.render_blocks(blocks[at:])
        else:
            body = self.render_blocks(blocks)
        footer = "".join(f"<p>{rich(p)}</p>" for p in (s["footer"] if isinstance(s.get("footer"), list) else [s.get("footer", "")]) if p)
        script = f"<script>{TAB_JS}</script>" if self.has_tabs else ""
        if self.panorama:
            script += f"<script>{PANORAMA_PARENT_JS}</script>"
        return (
            f'<!doctype html><html lang="ja" data-mode="{mode}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(s['title'])}</title>{FONT_LINKS}<style>\n{css}\n</style></head>"
            f'<body><div class="wrap">{header}{body}<footer>{footer}</footer></div>{script}</body></html>'
        )


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="ヤマビヨリのレポートをHTMLにする(仕様JSON、または --json-out の数値データから)")
    ap.add_argument("spec", nargs="?", help="レポート仕様 JSON (UTF-8)。--data を使うときは不要")
    ap.add_argument("--data", help="--json-out で保存した数値データ(診断・探索)。表・集計・出典は自動で組み立てる")
    ap.add_argument("--commentary", help="--data と併用: 解説 JSON(天気図の読み取りなど。省略可。形は scripts/report_baseline.py 冒頭)")
    ap.add_argument("--synoptic", help="--data と併用: scripts/fetch_synoptic.py の出力 JSON(天気図・ひまわり。省略可)")
    ap.add_argument("--days", type=int, default=7, help="--data(探索)で日別タブに出す日数(既定7、0=全日)")
    ap.add_argument("--top", type=int, default=15, help="--data(探索)で日ごとに出す上位の山数(既定15、0=全件)")
    ap.add_argument("--out", required=True, help="出力する HTML のパス")
    ap.add_argument("--embed-images", action="store_true", help="画像を base64 で埋め込み、HTML 1ファイルで完結させる")
    ap.add_argument("--base-dir", help="画像の相対パスの基準(既定: JSON のあるフォルダ)")
    ap.add_argument("--panorama-html", metavar="HTML",
                    help="山頂の展望図(mountain_weather_detail.py --panorama の HTML)。渡したときだけ、総評のあとに展望の節を足す。"
                         "--panorama-viewpoint-html も渡したときは、展望地の図のほうを載せる")
    ap.add_argument("--panorama-viewpoint-html", metavar="HTML",
                    help="展望地の展望図(--panorama の、名前の後ろに v の付く HTML)。載せる図は1枚で、これを渡したときはこちらを載せる")
    args = ap.parse_args(argv)
    if bool(args.spec) == bool(args.data):
        ap.error("レポート仕様 JSON(位置引数)か --data のどちらか一方を指定してください")

    src_path = Path(args.data or args.spec)
    try:
        if args.data:
            from report_baseline import build_spec
            spec = build_spec(_load_json(args.data),
                              commentary=_load_json(args.commentary) if args.commentary else None,
                              synoptic=_load_json(args.synoptic) if args.synoptic else None,
                              n_days=args.days or 999, top=args.top or 9999)
        else:
            spec = _load_json(args.spec)
        panorama = None  # 載せるのは1枚だけ。展望地の図があればそれを、無ければ山頂の図を
        if args.panorama_viewpoint_html:
            panorama = load_panorama(args.panorama_viewpoint_html, "viewpoint")
        elif args.panorama_html:
            panorama = load_panorama(args.panorama_html, "summit")
        r = Renderer(spec, args.base_dir or src_path.parent, args.embed_images, panorama)
        page = r.render()
    except (SpecError, KeyError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"エラー: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    for w in r.warnings:
        print(f"警告: {w}", file=sys.stderr)
    print(f"書き出しました: {out} ({len(page):,} 文字)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
