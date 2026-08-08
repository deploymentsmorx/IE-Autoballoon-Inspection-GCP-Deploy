import os

import streamlit as st

from autoballoon import (
    add_manual_characteristic,
    audit_family_counts,
    audit_pattern_counts,
    audit_pdf,
    audit_reason_counts,
    balloon_pdf,
    balloon_pdf_crosscheck,
    characteristics_to_csv,
    collect_miss_candidates,
    redraw_balloons,
)
from learning_store import (
    add_feedback,
    feedback_counts,
    load_feedback,
    store_path,
)

st.set_page_config(page_title="Auto Balloon", layout="centered")

st.title("Auto Balloon")
st.caption(
    "Vector detection first (zones + tiles). Optional AI fallback on missed spans. "
    "Manual balloons are saved so the next run learns them — no full-page OCR."
)

uploaded = st.file_uploader("Choose a drawing PDF", type=["pdf"])
audit_mode = st.toggle(
    "Audit mode (decision trail CSV — no balloons)",
    value=False,
    help="Logs raw → merged → pattern → accept/reject + reason for every text span.",
)

if uploaded is None:
    st.info("Drop a PDF to auto-balloon its dimensions.")
    counts = feedback_counts()
    if counts["total"]:
        st.caption(
            f"Learning store: {counts['dimension']} dimensions, "
            f"{counts['ignore']} ignore · `{store_path().name}`"
        )
    st.stop()

st.write(f"**File:** {uploaded.name}  ·  **Size:** {uploaded.size / 1024:.1f} KB")

cross_passes = 1
use_zones = True
use_tiles = True
use_learning = True
ai_fallback = False
ai_provider = "openai"
ai_api_key = ""
ai_model = ""

if not audit_mode:
    cross_passes = st.slider(
        "Cross-check passes",
        min_value=1,
        max_value=5,
        value=st.session_state.get("cross_passes", 1),
        help=(
            "1 = standard balloon once. "
            "2–5 = re-scan with wider merge each round to recover missed dims."
        ),
    )
    st.session_state["cross_passes"] = cross_passes
    use_zones = st.toggle(
        "Zone-aware (drawing / BOM / notes / title)",
        value=st.session_state.get("use_zones", True),
    )
    st.session_state["use_zones"] = use_zones
    use_tiles = st.toggle(
        "Tile drawing zone (3×4, ~0.5 in overlap)",
        value=st.session_state.get("use_tiles", True),
        help="Vector tiles only — denser grid + larger overlap to catch edge dims.",
    )
    st.session_state["use_tiles"] = use_tiles
    use_learning = st.toggle(
        "Use human learning store",
        value=st.session_state.get("use_learning", True),
        help="Auto-balloon texts you previously marked as dimensions.",
    )
    st.session_state["use_learning"] = use_learning

    with st.expander("AI fallback (OpenAI / Claude)", expanded=False):
        ai_fallback = st.toggle(
            "Enable AI fallback on missed spans",
            value=st.session_state.get("ai_fallback", False),
            help="After vector+learning, classify remaining digit spans with AI (text only).",
        )
        st.session_state["ai_fallback"] = ai_fallback
        ai_provider = st.selectbox(
            "Provider",
            options=["openai", "claude"],
            index=0 if st.session_state.get("ai_provider", "openai") == "openai" else 1,
        )
        st.session_state["ai_provider"] = ai_provider
        default_key = (
            os.environ.get("OPENAI_API_KEY", "")
            if ai_provider == "openai"
            else os.environ.get("ANTHROPIC_API_KEY", "")
        )
        ai_api_key = st.text_input(
            "API key",
            value=st.session_state.get("ai_api_key", default_key),
            type="password",
            help="Or set OPENAI_API_KEY / ANTHROPIC_API_KEY in the environment.",
        )
        st.session_state["ai_api_key"] = ai_api_key
        ai_model = st.text_input(
            "Model (optional)",
            value=st.session_state.get("ai_model", ""),
            placeholder="gpt-4o-mini or claude-sonnet-4-20250514",
        )
        st.session_state["ai_model"] = ai_model

run_label = "Run Audit" if audit_mode else "Auto Balloon Dimensions"
if st.button(run_label, type="primary", use_container_width=True):
    pdf_bytes = uploaded.getvalue()
    st.session_state["pdf_name"] = uploaded.name
    st.session_state["source_pdf"] = pdf_bytes

    if audit_mode:
        with st.status("Auditing text spans…", expanded=True) as status:
            rows, csv_bytes = audit_pdf(pdf_bytes)
            st.session_state["audit_rows"] = rows
            st.session_state["audit_csv"] = csv_bytes
            st.session_state.pop("ballooned_pdf", None)
            st.session_state.pop("chars", None)
            st.session_state.pop("pass_log", None)
            st.session_state["cross_level_next"] = 2
            accepted_n = sum(1 for r in rows if r["accepted"])
            status.update(
                label=f"Audit done — {accepted_n}/{len(rows)} spans accepted",
                state="complete",
            )
    else:
        extras = []
        if use_zones:
            extras.append("zones")
        if use_tiles:
            extras.append("tiles")
        if use_learning:
            extras.append("learning")
        if ai_fallback:
            extras.append(f"ai:{ai_provider}")
        with st.status(
            f"Detecting dimensions ({cross_passes} pass"
            f"{'es' if cross_passes > 1 else ''}"
            f"{', ' + ', '.join(extras) if extras else ''})…",
            expanded=True,
        ) as status:
            try:
                out_pdf, chars, pass_log = balloon_pdf(
                    pdf_bytes,
                    passes=cross_passes,
                    use_zones=use_zones,
                    use_tiles=use_tiles,
                    use_learning=use_learning,
                    ai_fallback=ai_fallback,
                    ai_provider=ai_provider,
                    ai_api_key=ai_api_key or None,
                    ai_model=ai_model or None,
                )
            except Exception as e:
                status.update(label=f"Failed: {e}", state="error")
                st.error(str(e))
                st.stop()
            st.session_state["ballooned_pdf"] = out_pdf
            st.session_state["chars"] = chars
            st.session_state["pass_log"] = pass_log
            st.session_state["cross_level_next"] = min(
                5, (pass_log[-1]["level"] if pass_log else 1) + 1
            )
            st.session_state.pop("audit_rows", None)
            st.session_state.pop("audit_csv", None)
            for entry in pass_log:
                mode = entry.get("mode", "full")
                st.write(
                    f"Pass {entry['pass']} ({mode}, level {entry['level']}): "
                    f"+{entry['added']} → total {entry['total']}"
                )
            status.update(
                label=f"Done — {len(chars)} dimension(s) ballooned",
                state="complete",
            )

if "audit_rows" in st.session_state:
    rows = st.session_state["audit_rows"]
    csv_bytes = st.session_state["audit_csv"]
    base = st.session_state.get("pdf_name", "drawing.pdf").rsplit(".", 1)[0]
    accepted_n = sum(1 for r in rows if r["accepted"])
    counts = audit_reason_counts(rows)

    st.success(f"Audit: {accepted_n}/{len(rows)} spans accepted")
    st.download_button(
        "Download audit CSV",
        data=csv_bytes,
        file_name=f"{base}_audit.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.subheader("Reject / accept reasons")
    st.dataframe(
        [{"reason": r, "count": n} for r, n in counts],
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Family breakdown (accepted)")
    st.dataframe(
        [{"family": f, "count": n} for f, n in audit_family_counts(rows)],
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Pattern breakdown (accepted)")
    st.dataframe(
        [{"pattern": p, "count": n} for p, n in audit_pattern_counts(rows)],
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Sample rows")
    st.dataframe(rows[:200], use_container_width=True, hide_index=True)

if "ballooned_pdf" in st.session_state:
    chars = st.session_state["chars"]
    out_pdf = st.session_state["ballooned_pdf"]
    base = st.session_state.get("pdf_name", "drawing.pdf").rsplit(".", 1)[0]
    pass_log = st.session_state.get("pass_log") or []

    st.success(f"{len(chars)} dimensions ballooned")

    if pass_log:
        st.subheader("Pass breakdown")
        st.dataframe(
            [
                {
                    "Pass": e["pass"],
                    "Mode": e.get("mode", "full"),
                    "Level": e["level"],
                    "Added": e["added"],
                    "Total": e["total"],
                }
                for e in pass_log
            ],
            use_container_width=True,
            hide_index=True,
        )

    if "source_pdf" in st.session_state and not audit_mode:
        st.divider()
        st.markdown("**Cross-check missed balloons**")
        extra_rounds = st.number_input(
            "Additional cross-check rounds",
            min_value=1,
            max_value=4,
            value=1,
        )
        if st.button("Cross-check again", use_container_width=True):
            with st.status("Cross-checking…", expanded=True) as status:
                start_lvl = int(st.session_state.get("cross_level_next", 2))
                out_pdf, chars, more_log = balloon_pdf_crosscheck(
                    st.session_state["source_pdf"],
                    st.session_state["chars"],
                    rounds=int(extra_rounds),
                    start_level=start_lvl,
                    use_zones=st.session_state.get("use_zones", True),
                    use_tiles=st.session_state.get("use_tiles", True),
                    use_learning=st.session_state.get("use_learning", True),
                    ai_fallback=st.session_state.get("ai_fallback", False),
                    ai_provider=st.session_state.get("ai_provider", "openai"),
                    ai_api_key=st.session_state.get("ai_api_key") or None,
                    ai_model=st.session_state.get("ai_model") or None,
                )
                st.session_state["ballooned_pdf"] = out_pdf
                st.session_state["chars"] = chars
                st.session_state["pass_log"] = list(pass_log) + [
                    {
                        "pass": (pass_log[-1]["pass"] if pass_log else 0) + e["pass"],
                        "level": e["level"],
                        "added": e["added"],
                        "total": e["total"],
                        "mode": e.get("mode", "full"),
                    }
                    for e in more_log
                ]
                st.session_state["cross_level_next"] = min(
                    5, (more_log[-1]["level"] if more_log else start_lvl) + 1
                )
                status.update(
                    label=f"Cross-check done — {len(chars)} total",
                    state="complete",
                )
            st.rerun()

        # ---- Human-in-the-loop ----
        st.divider()
        st.subheader("Human-in-the-loop (teach the system)")
        st.caption(
            "Mark missed dimensions so the next run balloons them automatically. "
            "Learning is saved under data/learned_feedback.json."
        )
        fc = feedback_counts()
        st.write(
            f"Store: **{fc['dimension']}** dimension examples, "
            f"**{fc['ignore']}** ignore · file `{store_path()}`"
        )

        if st.button("Load missed candidates from PDF", use_container_width=True):
            import fitz

            doc = fitz.open(stream=st.session_state["source_pdf"], filetype="pdf")
            try:
                misses = collect_miss_candidates(doc, st.session_state["chars"])
            finally:
                doc.close()
            st.session_state["miss_candidates"] = misses
            st.success(f"Loaded {len(misses)} candidate spans")

        misses = st.session_state.get("miss_candidates") or []
        if misses:
            st.dataframe(
                [
                    {
                        "page": m["page"],
                        "text": m["text"],
                        "zone": m["zone"],
                        "reason": m["reason"],
                        "x": round(m["x"], 1),
                        "y": round(m["y"], 1),
                    }
                    for m in misses[:80]
                ],
                use_container_width=True,
                hide_index=True,
            )
            pick = st.number_input(
                "Candidate row index (0-based from table above)",
                min_value=0,
                max_value=max(0, len(misses) - 1),
                value=0,
            )
            c_teach1, c_teach2, c_teach3 = st.columns(3)
            if c_teach1.button("Teach: is dimension", use_container_width=True):
                m = misses[int(pick)]
                add_feedback(
                    text=m["text"],
                    label="dimension",
                    page=m["page"],
                    x=m["x"],
                    y=m["y"],
                    width=m["width"],
                    height=m["height"],
                    neighbors=m.get("neighbors") or "",
                    source="manual",
                )
                new_chars = add_manual_characteristic(
                    st.session_state["chars"],
                    page=m["page"],
                    text=m["text"],
                    x=m["x"],
                    y=m["y"],
                    width=m["width"],
                    height=m["height"],
                )
                st.session_state["chars"] = new_chars
                st.session_state["ballooned_pdf"] = redraw_balloons(
                    st.session_state["source_pdf"], new_chars
                )
                st.success(f"Learned dimension {m['text']!r} and re-ballooned")
                st.rerun()
            if c_teach2.button("Teach: ignore", use_container_width=True):
                m = misses[int(pick)]
                add_feedback(
                    text=m["text"],
                    label="ignore",
                    page=m["page"],
                    x=m["x"],
                    y=m["y"],
                    width=m["width"],
                    height=m["height"],
                    neighbors=m.get("neighbors") or "",
                    source="manual",
                )
                st.success(f"Learned ignore {m['text']!r}")
            if c_teach3.button("Clear candidate list", use_container_width=True):
                st.session_state.pop("miss_candidates", None)
                st.rerun()

        with st.expander("Add dimension manually (page / text / x / y)"):
            mp = st.number_input("Page", min_value=1, value=1)
            mt = st.text_input("Dimension text", value="")
            mx = st.number_input("X (PDF points)", value=100.0)
            my = st.number_input("Y (PDF points)", value=100.0)
            if st.button("Add manual balloon + learn", use_container_width=True):
                if not mt.strip():
                    st.warning("Enter dimension text")
                else:
                    add_feedback(
                        text=mt.strip(),
                        label="dimension",
                        page=int(mp),
                        x=float(mx),
                        y=float(my),
                        source="manual",
                    )
                    new_chars = add_manual_characteristic(
                        st.session_state["chars"],
                        page=int(mp),
                        text=mt.strip(),
                        x=float(mx),
                        y=float(my),
                    )
                    st.session_state["chars"] = new_chars
                    st.session_state["ballooned_pdf"] = redraw_balloons(
                        st.session_state["source_pdf"], new_chars
                    )
                    st.success("Manual balloon added and learned")
                    st.rerun()

        with st.expander("View learning store"):
            st.json(load_feedback()[:50])

    c1, c2 = st.columns(2)
    c1.download_button(
        "Download ballooned PDF",
        data=st.session_state["ballooned_pdf"],
        file_name=f"{base}_ballooned.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
    c2.download_button(
        "Download characteristic list (CSV)",
        data=characteristics_to_csv(st.session_state["chars"]),
        file_name=f"{base}_characteristics.csv",
        mime="text/csv",
        use_container_width=True,
    )
