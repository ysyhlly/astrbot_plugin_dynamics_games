"""Pillow editorial poster & story-relay comic-panel renderer for collaborative activities."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .models import CATEGORIES

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_FONT = (
    _PLUGIN_ROOT / "engines" / "rollpig" / "resource" / "font" / "荆南麦圆体.otf"
)

_CATEGORY_THEMES: dict[str, tuple[str, str, str]] = {
    # (accent_hex, soft_bg_hex, badge_label)
    "reading": ("#2E6F63", "#EAF4F1", "组织共读"),
    "matchmaking": ("#3D5A99", "#EDF2FA", "游戏约局"),
    "relay": ("#8B4D7A", "#F8EFF5", "接龙创作"),
    "challenge": ("#C26A2B", "#FDF3EA", "小型挑战"),
    "creation": ("#3B6E8C", "#EDF5FA", "共同制作"),
    "mini_game": ("#5A5E9A", "#F0F1FA", "局内对局"),
}


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if _BUNDLED_FONT.is_file():
        try:
            return ImageFont.truetype(str(_BUNDLED_FONT), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 8,
) -> list[str]:
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return []
    lines: list[str] = []
    for para in clean.split("\n"):
        if not para.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        buf = ""
        for ch in para:
            cand = buf + ch
            bbox = draw.textbbox((0, 0), cand, font=font)
            if bbox[2] - bbox[0] <= max_width or not buf:
                buf = cand
            else:
                lines.append(buf)
                buf = ch
                if len(lines) >= max_lines:
                    break
        if len(lines) >= max_lines:
            break
        if buf:
            lines.append(buf)
            if len(lines) >= max_lines:
                break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines and clean and len(clean) > sum(len(x) for x in lines):
        lines[-1] = lines[-1].rstrip()[:-1] + "…" if len(lines[-1]) > 1 else "…"
    return lines


def _fit_artwork(image_path: str | Path, target_w: int, target_h: int) -> Image.Image | None:
    path = Path(image_path)
    if not path.is_file():
        return None
    try:
        with Image.open(path) as raw:
            img = raw.convert("RGB")
            src_w, src_h = img.size
            if src_w <= 0 or src_h <= 0:
                return None
            scale = max(target_w / src_w, target_h / src_h)
            new_w = max(1, int(round(src_w * scale)))
            new_h = max(1, int(round(src_h * scale)))
            resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - target_w) // 2)
            top = max(0, (new_h - target_h) // 2)
            return resized.crop((left, top, left + target_w, top + target_h))
    except Exception:
        return None


MAX_POSTER_FILES = 100


def prune_old_posters(
    output_dir: Path | None = None,
    *,
    max_files: int = MAX_POSTER_FILES,
    keep: Path | None = None,
) -> int:
    """Keep at most ``max_files`` image files in the posters directory (oldest evicted first)."""
    target_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    if not target_dir.is_dir():
        return 0
    cap = max(1, int(max_files))
    keep_resolved = None
    if keep is not None:
        try:
            keep_resolved = Path(keep).resolve()
        except OSError:
            keep_resolved = None
    entries: list[tuple[float, Path]] = []
    try:
        for entry in target_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            entries.append((mtime, entry))
    except OSError:
        return 0

    if len(entries) <= cap:
        return 0
    entries.sort(key=lambda item: item[0])
    to_remove = len(entries) - cap
    removed = 0
    for _mtime, path in entries:
        if removed >= to_remove:
            break
        if keep_resolved is not None:
            try:
                if path.resolve() == keep_resolved:
                    continue
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def _default_output_dir() -> Path:
    try:
        from astrbot.api.star import StarTools

        from ..runtime import PLUGIN_ID

        base = Path(StarTools.get_data_dir(PLUGIN_ID)) / "posters"
    except Exception:
        base = _PLUGIN_ROOT / ".posters_tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def render_activity_poster(
    activity: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
    *,
    outcome: dict[str, Any] | None = None,
    cover_image_path: str = "",
    output_dir: Path | None = None,
) -> Path:
    """Render a shareable editorial poster PNG for a collaborative activity.

    Embeds ``cover_image_path`` when generated by Visual Media; otherwise renders
    a clean Pillow typographic poster with zero external dependencies.
    """
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    cat_key = str(activity.get("category") or "creation")
    accent, soft_bg, cat_badge = _CATEGORY_THEMES.get(
        cat_key, ("#3B6E8C", "#EDF5FA", CATEGORIES.get(cat_key, {}).get("label", "协作活动"))
    )

    width = 860
    pad = 44
    content_w = width - pad * 2
    paper_bg = "#FAF7F2"
    card_bg = "#FFFFFF"
    ink = "#1F242B"
    muted = "#616B76"
    line_color = "#E4DDD2"

    f_badge = _load_font(20)
    f_title = _load_font(36)
    f_meta = _load_font(20)
    f_sec = _load_font(24)
    f_body = _load_font(21)
    f_small = _load_font(18)

    # Measure content layout first on a scratch canvas
    scratch = Image.new("RGB", (width, 200), paper_bg)
    sdraw = ImageDraw.Draw(scratch)

    title_lines = _wrap_text(
        sdraw, str(activity.get("title") or "未命名协作活动"), f_title, content_w - 40, max_lines=3
    )
    summary_src = (
        str((outcome or {}).get("summary") or "").strip()
        or str(activity.get("description") or "").strip()
        or f"由 {activity.get('creator_name') or '群友'} 发起的{cat_badge}协作。"
    )
    summary_lines = _wrap_text(sdraw, summary_src, f_body, content_w - 48, max_lines=6)

    joined = [p for p in participants if p.get("status", "joined") == "joined"]
    roster_text = "、".join(
        str(p.get("name") or p.get("uid") or "") for p in joined[:18]
    ) or str(activity.get("creator_name") or "群友")
    if len(joined) > 18:
        roster_text += f" 等共 {len(joined)} 人"
    roster_lines = _wrap_text(sdraw, f"参与成员：{roster_text}", f_meta, content_w - 48, max_lines=3)

    stages_text = " → ".join(str(s) for s in (activity.get("stages") or []))
    stage_lines = _wrap_text(sdraw, f"阶段路线：{stages_text}", f_small, content_w - 48, max_lines=2)

    excerpt_blocks: list[tuple[str, list[str]]] = []
    for idx, c in enumerate(contributions[-6:], max(1, len(contributions) - 5)):
        header = f"#{idx} {c.get('name') or c.get('uid') or '群友'}"
        if c.get("stage_name"):
            header += f" · {c['stage_name']}"
        wrapped = _wrap_text(
            sdraw, str(c.get("content") or ""), f_body, content_w - 64, max_lines=3
        )
        if wrapped:
            excerpt_blocks.append((header, wrapped))

    cover_img = _fit_artwork(cover_image_path, content_w, 380) if cover_image_path else None

    # Compute exact height
    y = pad
    header_h = 46 + len(title_lines) * 46 + 44
    y += header_h + 22
    if cover_img is not None:
        y += 380 + 22
    summary_card_h = 34 + len(summary_lines) * 32 + 16 + len(roster_lines) * 30 + len(stage_lines) * 26 + 34
    y += summary_card_h + 22
    if excerpt_blocks:
        y += 40
        for _, lines in excerpt_blocks:
            y += 28 + len(lines) * 30 + 28
    y += 72  # footer

    canvas = Image.new("RGB", (width, max(620, y)), paper_bg)
    draw = ImageDraw.Draw(canvas)

    # Top accent stripe
    draw.rectangle([(0, 0), (width, 10)], fill=accent)

    cur_y = pad
    # Header card
    draw.rounded_rectangle(
        [(pad, cur_y), (width - pad, cur_y + header_h)],
        radius=18,
        fill=soft_bg,
        outline=line_color,
        width=2,
    )
    badge_text = f"{cat_badge} · {activity.get('id', '')}"
    draw.rounded_rectangle(
        [(pad + 20, cur_y + 18), (pad + 220, cur_y + 52)],
        radius=14,
        fill=accent,
    )
    draw.text((pad + 34, cur_y + 24), badge_text, fill="#FFFFFF", font=f_badge)

    status_label = str(activity.get("status_label") or activity.get("status") or "")
    draw.text((width - pad - 180, cur_y + 24), status_label, fill=accent, font=f_badge)

    ty = cur_y + 66
    for line in title_lines:
        draw.text((pad + 22, ty), line, fill=ink, font=f_title)
        ty += 46

    cur_y += header_h + 22

    # Optional cover artwork from Visual Media
    if cover_img is not None:
        canvas.paste(cover_img, (pad, cur_y))
        draw.rectangle(
            [(pad, cur_y), (pad + content_w, cur_y + 380)],
            outline=line_color,
            width=2,
        )
        cur_y += 380 + 22

    # Summary + roster card
    draw.rounded_rectangle(
        [(pad, cur_y), (width - pad, cur_y + summary_card_h)],
        radius=16,
        fill=card_bg,
        outline=line_color,
        width=2,
    )
    sy = cur_y + 22
    for line in summary_lines:
        draw.text((pad + 24, sy), line, fill=ink, font=f_body)
        sy += 32
    sy += 12
    draw.line([(pad + 24, sy), (width - pad - 24, sy)], fill=line_color, width=1)
    sy += 14
    for line in roster_lines:
        draw.text((pad + 24, sy), line, fill=muted, font=f_meta)
        sy += 30
    for line in stage_lines:
        draw.text((pad + 24, sy), line, fill=muted, font=f_small)
        sy += 26

    cur_y += summary_card_h + 22

    # Highlights / Excerpts
    if excerpt_blocks:
        sec_title = "接龙精彩片段" if cat_key == "relay" else "协作精彩记录"
        draw.text((pad + 6, cur_y), f"▎{sec_title}", fill=accent, font=f_sec)
        cur_y += 38
        for hdr, lines in excerpt_blocks:
            bh = 26 + len(lines) * 30 + 20
            draw.rounded_rectangle(
                [(pad, cur_y), (width - pad, cur_y + bh)],
                radius=14,
                fill=card_bg,
                outline=line_color,
                width=1,
            )
            draw.text((pad + 22, cur_y + 12), hdr, fill=accent, font=f_small)
            ly = cur_y + 38
            for ln in lines:
                draw.text((pad + 22, ly), ln, fill=ink, font=f_body)
                ly += 30
            cur_y += bh + 12

    # Footer
    footer_y = canvas.size[1] - 44
    draw.line([(pad, footer_y - 12), (width - pad, footer_y - 12)], fill=line_color, width=1)
    src_tag = "AI 视觉配图 + 群间画报" if cover_img is not None else "群间精致画报"
    draw.text(
        (pad, footer_y),
        f"群间游戏 Dynamics Games · {src_tag}",
        fill=muted,
        font=f_small,
    )
    draw.text(
        (width - pad - 220, footer_y),
        f"参与 {len(joined)} 人 · 记录 {len(contributions)} 条",
        fill=muted,
        font=f_small,
    )

    digest = hashlib.sha256(
        f"{activity.get('id', '')}:{len(contributions)}:{time.time_ns()}".encode()
    ).hexdigest()[:10]
    out_path = out_dir / f"poster_{activity.get('id', 'act')}_{digest}.png"
    canvas.save(out_path, format="PNG")
    prune_old_posters(out_dir, keep=out_path)
    return out_path


def render_relay_illustration_card(
    activity: dict[str, Any],
    turn_number: int,
    author_name: str,
    excerpt: str,
    *,
    artwork_path: str = "",
    output_dir: Path | None = None,
) -> Path:
    """Render a compact comic-panel card for every Nth turn in a story relay."""
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    accent, soft_bg, _ = _CATEGORY_THEMES["relay"]
    width = 760
    pad = 36
    content_w = width - pad * 2
    paper_bg = "#FAF6FA"
    ink = "#211F24"
    muted = "#6A5D6E"
    line_color = "#E3D6E5"

    f_title = _load_font(28)
    f_meta = _load_font(19)
    f_body = _load_font(22)

    scratch = Image.new("RGB", (width, 200), paper_bg)
    sdraw = ImageDraw.Draw(scratch)
    text_lines = _wrap_text(sdraw, excerpt, f_body, content_w - 40, max_lines=5)
    art = _fit_artwork(artwork_path, content_w, 340) if artwork_path else None

    total_h = pad + 78 + (340 + 18 if art is not None else 0) + (len(text_lines) * 32 + 44) + 56
    canvas = Image.new("RGB", (width, max(320, total_h)), paper_bg)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 0), (width, 8)], fill=accent)

    y = pad
    draw.rounded_rectangle(
        [(pad, y), (width - pad, y + 66)],
        radius=14,
        fill=soft_bg,
        outline=line_color,
        width=2,
    )
    draw.text(
        (pad + 20, y + 14),
        f"✍️ 接龙连环画 · 第 {turn_number} 棒",
        fill=accent,
        font=f_title,
    )
    y += 80

    if art is not None:
        canvas.paste(art, (pad, y))
        draw.rectangle([(pad, y), (pad + content_w, y + 340)], outline=line_color, width=2)
        y += 358

    box_h = max(68, len(text_lines) * 32 + 34)
    draw.rounded_rectangle(
        [(pad, y), (width - pad, y + box_h)],
        radius=14,
        fill="#FFFFFF",
        outline=line_color,
        width=2,
    )
    ly = y + 18
    for ln in text_lines:
        draw.text((pad + 20, ly), ln, fill=ink, font=f_body)
        ly += 32

    footer_y = canvas.size[1] - 38
    draw.text(
        (pad, footer_y),
        f"《{activity.get('title', '')}》 · 执笔：{author_name}",
        fill=muted,
        font=f_meta,
    )

    digest = hashlib.sha256(
        f"relay:{activity.get('id', '')}:{turn_number}:{time.time_ns()}".encode()
    ).hexdigest()[:10]
    out_path = out_dir / f"relay_{activity.get('id', 'act')}_t{turn_number}_{digest}.png"
    canvas.save(out_path, format="PNG")
    prune_old_posters(out_dir, keep=out_path)
    return out_path


def render_telephone_step_card(
    room_title: str,
    step_no: int,
    prompt_text: str,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Pillow fallback card for one AI Telephone image step when Visual Media is unavailable."""
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    width, height = 720, 420
    canvas = Image.new("RGB", (width, height), "#F7F4FB")
    draw = ImageDraw.Draw(canvas)
    accent = "#6C4C9E"
    draw.rectangle([(0, 0), (width, 10)], fill=accent)

    f_hdr = _load_font(26)
    f_body = _load_font(24)
    f_small = _load_font(18)

    draw.rounded_rectangle(
        [(36, 36), (width - 36, height - 36)],
        radius=20,
        fill="#FFFFFF",
        outline="#DDD4EC",
        width=2,
    )
    draw.text((60, 56), f"🎨 AI 传话筒 · 第 {step_no} 步画板（离线视觉降级）", fill=accent, font=f_hdr)

    # Stylized abstract sketch hints (geometric composition seeded by prompt)
    seed_int = int(hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:8], 16)
    cx = 160 + (seed_int % 380)
    cy = 185 + ((seed_int >> 4) % 70)
    draw.ellipse([(cx - 48, cy - 48), (cx + 48, cy + 48)], fill="#EFE8FA", outline=accent, width=3)
    draw.rounded_rectangle([(80, 140), (width - 80, 290)], radius=14, outline="#E3DCF0", width=2)

    lines = _wrap_text(draw, f"画面意象：{prompt_text}", f_body, width - 160, max_lines=4)
    ly = 158
    for ln in lines:
        draw.text((100, ly), ln, fill="#2A2433", font=f_body)
        ly += 34

    draw.text(
        (60, height - 76),
        f"房间《{room_title}》· 下家请根据意象脑补并发送 `/传话筒 提交 <描述>`",
        fill="#6D637A",
        font=f_small,
    )

    digest = hashlib.sha256(f"tel-step:{step_no}:{prompt_text}:{time.time_ns()}".encode()).hexdigest()[:10]
    out_path = out_dir / f"telephone_step_{step_no}_{digest}.png"
    canvas.save(out_path, format="PNG")
    prune_old_posters(out_dir, keep=out_path)
    return out_path


def render_telephone_strip(
    title: str,
    turns: list[dict[str, Any]],
    *,
    output_dir: Path | None = None,
) -> Path:
    """Stitch an entire AI Telephone chain (text -> image -> text -> ...) into one vertical strip."""
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    width = 820
    pad = 40
    content_w = width - pad * 2
    paper_bg = "#FAF7FC"
    accent = "#6C4C9E"
    line_color = "#DFD5EC"
    ink = "#211E28"
    muted = "#685E77"

    f_title = _load_font(32)
    f_hdr = _load_font(20)
    f_body = _load_font(22)
    f_small = _load_font(18)

    scratch = Image.new("RGB", (width, 120), paper_bg)
    sdraw = ImageDraw.Draw(scratch)

    rendered_blocks: list[dict[str, Any]] = []
    total_h = pad + 92
    for idx, t in enumerate(turns[:16], 1):
        kind = str(t.get("kind") or "text")
        author = str(t.get("author_name") or t.get("author_uid") or "AI 画师")
        if kind == "image":
            img = _fit_artwork(str(t.get("image_path") or ""), content_w - 32, 300)
            if img is not None:
                bh = 44 + 300 + 20
                rendered_blocks.append({"kind": "image", "idx": idx, "author": author, "img": img, "h": bh})
                total_h += bh + 16
                continue
        lines = _wrap_text(
            sdraw,
            str(t.get("text") or "（无内容）"),
            f_body,
            content_w - 44,
            max_lines=4,
        )
        bh = 42 + max(1, len(lines)) * 32 + 22
        rendered_blocks.append({"kind": "text", "idx": idx, "author": author, "lines": lines, "h": bh})
        total_h += bh + 16

    total_h += 64
    canvas = Image.new("RGB", (width, max(400, total_h)), paper_bg)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 0), (width, 10)], fill=accent)

    y = pad
    draw.rounded_rectangle(
        [(pad, y), (width - pad, y + 76)],
        radius=16,
        fill="#F0E9FA",
        outline=line_color,
        width=2,
    )
    draw.text((pad + 22, y + 18), f"📞 AI 传话筒歪楼全纪录 · {title[:22]}", fill=accent, font=f_title)
    y += 92

    for blk in rendered_blocks:
        bh = blk["h"]
        draw.rounded_rectangle(
            [(pad, y), (width - pad, y + bh)],
            radius=14,
            fill="#FFFFFF",
            outline=line_color,
            width=2,
        )
        tag = "🖼️ AI 绘画面" if blk["kind"] == "image" else "✍️ 群友描述"
        draw.text(
            (pad + 18, y + 12),
            f"第 {blk['idx']} 棒 · {tag} · {blk['author']}",
            fill=accent,
            font=f_hdr,
        )
        if blk["kind"] == "image":
            canvas.paste(blk["img"], (pad + 16, y + 44))
        else:
            ly = y + 46
            for ln in blk["lines"]:
                draw.text((pad + 22, ly), ln, fill=ink, font=f_body)
                ly += 32
        y += bh + 16

    draw.text(
        (pad, canvas.size[1] - 42),
        f"群间游戏 · AI 传话筒对比长图（共 {len(turns)} 棒）",
        fill=muted,
        font=f_small,
    )

    digest = hashlib.sha256(f"tel-strip:{title}:{len(turns)}:{time.time_ns()}".encode()).hexdigest()[:10]
    out_path = out_dir / f"telephone_strip_{digest}.png"
    canvas.save(out_path, format="PNG")
    prune_old_posters(out_dir, keep=out_path)
    return out_path


def render_pictionary_fallback_card(
    category: str,
    word_len: int,
    round_no: int,
    *,
    clue: str = "",
    seed_text: str = "",
    output_dir: Path | None = None,
) -> Path:
    """Pillow puzzle card fallback for Pictionary when Visual Media is unavailable (never leaks answer!)."""
    out_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    width, height = 720, 440
    canvas = Image.new("RGB", (width, height), "#FBF8F1")
    draw = ImageDraw.Draw(canvas)
    accent = "#C46A2B"
    draw.rectangle([(0, 0), (width, 10)], fill=accent)

    f_title = _load_font(28)
    f_big = _load_font(36)
    f_body = _load_font(22)
    f_small = _load_font(18)

    draw.rounded_rectangle(
        [(36, 36), (width - 36, height - 36)],
        radius=20,
        fill="#FFFFFF",
        outline="#EADCC8",
        width=2,
    )
    draw.text((60, 56), f"🎨 你画我猜 · 第 {round_no} 题（离线谜面卡）", fill=accent, font=f_title)
    draw.text(
        (60, 108),
        f"分类：{category}  ｜  字数：{word_len} 字",
        fill="#5E5247",
        font=f_body,
    )

    # Abstract visual motif (seeded by category + round, never includes answer text!)
    seed_int = int(hashlib.sha256(f"{category}:{round_no}:{seed_text}".encode()).hexdigest()[:8], 16)
    palette = ("#F3D5B5", "#D8E2DC", "#FFE5D9", "#D7E3FC")
    for i in range(4):
        rx = 90 + ((seed_int >> (i * 3)) % 460)
        ry = 155 + ((seed_int >> (i * 5)) % 90)
        r = 22 + ((seed_int >> (i * 2)) % 28)
        draw.ellipse([(rx - r, ry - r), (rx + r, ry + r)], fill=palette[i % len(palette)], outline=accent, width=2)

    blanks = "  ".join("□" for _ in range(max(1, min(8, word_len))))
    draw.text((60, 260), f"词长谜面：{blanks}", fill="#241E19", font=f_big)
    if clue:
        clue_lines = _wrap_text(draw, f"意象提示：{clue}", f_body, width - 120, max_lines=2)
        ly = 320
        for ln in clue_lines:
            draw.text((60, ly), ln, fill="#5E5247", font=f_body)
            ly += 30
    draw.text(
        (60, height - 72),
        "直接发送答案或 `/你画我猜 猜 <词>` 抢答 ｜ `/你画我猜 提示` 获取线索",
        fill="#8A7C70",
        font=f_small,
    )

    digest = hashlib.sha256(f"pic:{category}:{round_no}:{time.time_ns()}".encode()).hexdigest()[:10]
    out_path = out_dir / f"pictionary_r{round_no}_{digest}.png"
    canvas.save(out_path, format="PNG")
    prune_old_posters(out_dir, keep=out_path)
    return out_path
