import sqlite3
import re
import io
import math
import random
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, flash, Response, jsonify, url_for

# PDF Generation Libraries
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import legal
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing, Line

app = Flask(__name__)
app.secret_key = "super_secret_ring_key_1234"

CURRENT_DRAW_DATE = ""

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            super().showPage()
        super().save()

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica-Bold", 9)
        self.setFillColor(colors.black)
        self.restoreState()

def init_db():
    db_file = "ring_inventory.db"
    conn = sqlite3.connect(db_file, timeout=30.0)
    cursor = conn.cursor()
    
    # Enable WAL mode for multi-user concurrency across devices
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draw_date TEXT NOT NULL,
        ring_number TEXT NOT NULL,
        qty_1st INTEGER DEFAULT 0,
        qty_2nd INTEGER DEFAULT 0,
        sheet_type TEXT DEFAULT 'main',
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS draw_files (
        draw_date TEXT PRIMARY KEY,
        bond_name TEXT DEFAULT '',
        party_name TEXT DEFAULT '',
        party_code TEXT DEFAULT '',
        city TEXT DEFAULT '',
        is_hidden INTEGER DEFAULT 0,
        is_closed INTEGER DEFAULT 0,
        expire_time TEXT DEFAULT ''
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    cursor.execute("PRAGMA table_info(inventory)")
    inv_cols = [col[1] for col in cursor.fetchall()]
    if 'sheet_type' not in inv_cols:
        cursor.execute("ALTER TABLE inventory ADD COLUMN sheet_type TEXT DEFAULT 'main'")

    default_settings = [
        ('exp_limit_1st_1d', '700'), ('exp_limit_2nd_1d', '1000'),
        ('exp_limit_1st_2d', '250'), ('exp_limit_2nd_2d', '500'),
        ('exp_limit_1st_3d', '50'),  ('exp_limit_2nd_3d', '50'),
        ('exp_limit_1st_4d', '25'),  ('exp_limit_2nd_4d', '25'),
        ('imp_limit_1st_1d', '2000'), ('imp_limit_2nd_1d', '3000'),
        ('imp_limit_1st_2d', '1000'), ('imp_limit_2nd_2d', '1500'),
        ('imp_limit_1st_3d', '200'),  ('imp_limit_2nd_3d', '600'),
        ('imp_limit_1st_4d', '100'),  ('imp_limit_2nd_4d', '100'),
        ('sms_paste_enabled', '1')
    ]
    for key, val in default_settings:
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(val)))

    conn.commit()
    conn.close()

def get_limits():
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    
    res = {}
    for row in rows:
        key, val = row[0], row[1]
        try:
            res[key] = int(val)
        except (ValueError, TypeError):
            res[key] = val
    return res

def get_total_sales_breakdown(active_draw):
    if not active_draw:
        return {"main": 0, "overflow": 0, "filter": 0}
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    
    cursor.execute("SELECT SUM(qty_1st + qty_2nd) FROM inventory WHERE draw_date=? AND sheet_type='main'", (active_draw,))
    main_sale = cursor.fetchone()[0] or 0

    cursor.execute("SELECT SUM(qty_1st + qty_2nd) FROM inventory WHERE draw_date=? AND sheet_type LIKE 'overflow%'", (active_draw,))
    overflow_sale = cursor.fetchone()[0] or 0

    cursor.execute("SELECT SUM(qty_1st + qty_2nd) FROM inventory WHERE draw_date=? AND sheet_type LIKE 'filter%'", (active_draw,))
    filter_sale = cursor.fetchone()[0] or 0

    conn.close()
    return {
        "main": main_sale,
        "overflow": overflow_sale,
        "filter": filter_sale
    }

def get_remaining_capacity(active_draw, ring, limits):
    if not ring or not active_draw:
        return None, None
    ring = str(ring).strip().upper()
    if not (1 <= len(ring) <= 4):
        return None, None
    if not re.search(r'\d', ring) or re.search(r'[^0-9X]', ring):
        return None, None

    digits_only = [c for c in ring if c.isdigit()]
    d_len = len(digits_only)
    if d_len not in [1, 2, 3, 4]:
        return None, None

    exp_lim1 = limits.get(f'exp_limit_1st_{d_len}d', 0)
    exp_lim2 = limits.get(f'exp_limit_2nd_{d_len}d', 0)

    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(qty_1st), SUM(qty_2nd) FROM inventory WHERE draw_date=? AND ring_number=? AND sheet_type='main'", (active_draw, ring))
    row = cursor.fetchone()
    conn.close()

    curr_1st = row[0] or 0
    curr_2nd = row[1] or 0

    rem_1st = max(0, exp_lim1 - curr_1st)
    rem_2nd = max(0, exp_lim2 - curr_2nd)
    return rem_1st, rem_2nd

def parse_sms_text(text):
    results = []
    if not text:
        return results

    ascii_digits = "01234567890123456789"
    trans_table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", ascii_digits)
    text = text.translate(trans_table)

    lines = text.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        line_upper = line.upper()

        if re.search(r'[FS]\s*\d+', line_upper):
            f_match = re.search(r'[F]\s*(\d+)', line_upper)
            s_match = re.search(r'[S]\s*(\d+)', line_upper)
            q1 = int(f_match.group(1)) if f_match else 0
            q2 = int(s_match.group(1)) if s_match else 0

            nums_text = re.split(r'[FS]', line_upper)[0]
            raw_nums = re.findall(r'\b[0-9X]{1,4}\b', nums_text)
            for num in raw_nums:
                if re.search(r'\d', num) and not re.search(r'[^0-9X]', num):
                    results.append((num, q1, q2))
            continue

        norm_line = line_upper.replace(':', '=').replace('：', '=').replace('＝', '=')
        norm_line = re.sub(r'\s*=\s*', '=', norm_line)
        norm_line = re.sub(r'\s*/\s*', '/', norm_line)
        norm_line = re.sub(r'\s*\+\s*', '+', norm_line)
        norm_line = re.sub(r'([0-9X])\s*-\s*(\d)', r'\1-\2', norm_line)

        raw_tokens = re.split(r'[\s,;]+', norm_line)
        line_entries_found = False

        for token in raw_tokens:
            token = token.strip()
            if not token:
                continue

            match_a = re.match(r'^([0-9X\.\*]+)[=/\-+]+(\d+)(?:[=/\-+]+(\d+))?$', token)
            if match_a:
                rings_part = match_a.group(1)
                q1_val = int(match_a.group(2))
                q2_val = int(match_a.group(3)) if match_a.group(3) else 0

                nums = [n for n in re.split(r'[\.\*]', rings_part) if n]
                if nums:
                    for num in nums:
                        if 1 <= len(num) <= 4 and re.search(r'\d', num) and not re.search(r'[^0-9X]', num):
                            results.append((num, q1_val, q2_val))
                            line_entries_found = True

        if not line_entries_found:
            space_matches = re.finditer(r'(?<![0-9X])([0-9X\.\*]+)\s+(\d+)(?:\s+(\d+))?(?![0-9X])', norm_line)
            found_space = False
            for sm in space_matches:
                rings_part = sm.group(1)
                q1_val = int(sm.group(2))
                q2_val = int(sm.group(3)) if sm.group(3) else 0

                nums = [n for n in re.split(r'[\.\*]', rings_part) if n]
                if nums:
                    for num in nums:
                        if 1 <= len(num) <= 4 and re.search(r'\d', num) and not re.search(r'[^0-9X]', num):
                            results.append((num, q1_val, q2_val))
                            found_space = True

            if not found_space:
                global_matches = re.finditer(r'([0-9X\.\*]+)\s*[:=\-\/]\s*(\d+)(?:\s*[\/\-\+]\s*(\d+))?', norm_line)
                for gm in global_matches:
                    rings_part = gm.group(1)
                    q1_val = int(gm.group(2))
                    q2_val = int(gm.group(3)) if gm.group(3) else 0

                    nums = [n for n in re.split(r'[\.\*]', rings_part) if n]
                    for num in nums:
                        if 1 <= len(num) <= 4 and re.search(r'\d', num) and not re.search(r'[^0-9X]', num):
                            results.append((num, q1_val, q2_val))

    return results

def process_single_entry(cursor, active_draw, ring, req_q1, req_q2, limits):
    ring = str(ring).strip().upper()

    if not (1 <= len(ring) <= 4):
        return False, f"Ring '{ring}' exceeds maximum allowed length of 4 characters"
    if not re.search(r'\d', ring) or re.search(r'[^0-9X]', ring):
        return False, f"Invalid ring format for '{ring}'"

    digits_only = [c for c in ring if c.isdigit()]
    digit_len = len(digits_only)
    if digit_len not in [1, 2, 3, 4]:
        return False, f"Invalid ring digits for {ring}"

    req_q1 = max(0, int(req_q1))
    req_q2 = max(0, int(req_q2))

    if req_q1 == 0 and req_q2 == 0:
        return True, "Success"

    exp_lim1 = limits.get(f'exp_limit_1st_{digit_len}d', 0)
    exp_lim2 = limits.get(f'exp_limit_2nd_{digit_len}d', 0)
    imp_lim1 = limits.get(f'imp_limit_1st_{digit_len}d', 0)
    imp_lim2 = limits.get(f'imp_limit_2nd_{digit_len}d', 0)

    cursor.execute("SELECT SUM(qty_1st), SUM(qty_2nd) FROM inventory WHERE draw_date=? AND ring_number=? AND sheet_type='main'", (active_draw, ring))
    main_res = cursor.fetchone()
    curr_exp1 = main_res[0] or 0
    curr_exp2 = main_res[1] or 0

    cursor.execute("SELECT SUM(qty_1st), SUM(qty_2nd) FROM inventory WHERE draw_date=? AND ring_number=? AND sheet_type LIKE 'overflow%'", (active_draw, ring))
    imp_res = cursor.fetchone()
    curr_imp1 = imp_res[0] or 0
    curr_imp2 = imp_res[1] or 0

    fill_exp1 = min(req_q1, max(0, exp_lim1 - curr_exp1))
    fill_exp2 = min(req_q2, max(0, exp_lim2 - curr_exp2))

    rem_q1 = req_q1 - fill_exp1
    rem_q2 = req_q2 - fill_exp2

    if fill_exp1 > 0 or fill_exp2 > 0:
        cursor.execute("INSERT INTO inventory (draw_date, ring_number, qty_1st, qty_2nd, sheet_type) VALUES (?, ?, ?, ?, 'main')",
                       (active_draw, ring, fill_exp1, fill_exp2))

    fill_imp1 = min(rem_q1, max(0, imp_lim1 - curr_imp1))
    fill_imp2 = min(rem_q2, max(0, imp_lim2 - curr_imp2))

    rem_filter_q1 = rem_q1 - fill_imp1
    rem_filter_q2 = rem_q2 - fill_imp2

    if fill_imp1 > 0 or fill_imp2 > 0:
        cursor.execute("INSERT INTO inventory (draw_date, ring_number, qty_1st, qty_2nd, sheet_type) VALUES (?, ?, ?, ?, 'overflow_1')",
                       (active_draw, ring, fill_imp1, fill_imp2))

    if rem_filter_q1 > 0 or rem_filter_q2 > 0:
        cursor.execute("INSERT INTO inventory (draw_date, ring_number, qty_1st, qty_2nd, sheet_type) VALUES (?, ?, ?, ?, 'filter_1')",
                       (active_draw, ring, rem_filter_q1, rem_filter_q2))

    return True, "Success"

def check_file_is_closed(active_draw):
    if not active_draw:
        return True, "No active file selected."
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT is_closed, expire_time FROM draw_files WHERE draw_date=?", (active_draw,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return True, "File not found."
    is_closed, expire_time_str = row[0], row[1]
    if is_closed == 1:
        conn.close()
        return True, "سیل بند ہو گئی ہے"
    if expire_time_str and expire_time_str.strip():
        try:
            expire_dt = datetime.strptime(expire_time_str.strip(), "%Y-%m-%d %H:%M")
            if datetime.now() >= expire_dt:
                cursor.execute("UPDATE draw_files SET is_closed=1 WHERE draw_date=?", (active_draw,))
                conn.commit()
                conn.close()
                return True, "سیل بند ہو گئی ہے"
        except ValueError:
            pass
    conn.close()
    return False, ""

def get_visible_draw_files():
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT draw_date, bond_name, party_name, party_code, city, is_hidden, is_closed, expire_time FROM draw_files WHERE is_hidden=0")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_draw_files():
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT draw_date, bond_name, party_name, party_code, city, is_hidden, is_closed, expire_time FROM draw_files")
    rows = cursor.fetchall()
    conn.close()
    return rows

def is_advance_draw(active_draw):
    """Checks if the currently active draw date is later than any other visible draw date."""
    if not active_draw:
        return False
    try:
        active_dt = datetime.strptime(active_draw.strip(), "%d/%m/%Y")
    except ValueError:
        return False
    
    visible_files = get_visible_draw_files()
    for f in visible_files:
        f_date = f[0]
        if f_date == active_draw:
            continue
        try:
            f_dt = datetime.strptime(f_date.strip(), "%d/%m/%Y")
            if active_dt > f_dt:
                return True
        except ValueError:
            continue
    return False

def generate_pdf_report(active_draw, sheet_filter="main", target_ring=""):
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    
    cursor.execute("SELECT bond_name, party_name, party_code, city FROM draw_files WHERE draw_date=?", (active_draw,))
    f_info = cursor.fetchone() or ("", "", "", "")

    buffer = io.BytesIO()
    
    top_margin = 72
    side_bottom_margin = 36

    doc = SimpleDocTemplate(
        buffer, 
        pagesize=legal, 
        rightMargin=side_bottom_margin, 
        leftMargin=side_bottom_margin, 
        topMargin=top_margin, 
        bottomMargin=side_bottom_margin
    )
    story = []

    col_widths = [26, 60, 45, 45, 6, 60, 45, 45, 6, 60, 45, 45]
    total_table_width = sum(col_widths)

    def build_single_voucher_story(data_subset, sheet_label_str, is_overflow_sheet=False, is_filter=False):
        sub_story = []
        if is_filter:
            sheet_box_html = f'<b><font color="red">{sheet_label_str}</font></b>'
        elif is_overflow_sheet:
            sheet_box_html = f'<b>{sheet_label_str}</b>'
        else:
            sheet_box_html = f'<b>{sheet_label_str}</b>'

        header_table_data = [
            [
                Paragraph(f"<b>Date:</b> {active_draw}<br/><b>Bond:</b> {f_info[0]}", ParagraphStyle('H1', fontName='Helvetica-Bold', fontSize=10, leading=13)),
                Paragraph(f"<b>Party Name:</b> {f_info[1]}", ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=10, leading=12)),
                Paragraph(f"<b>City:</b> {f_info[3]}", ParagraphStyle('H3', fontName='Helvetica-Bold', fontSize=10, leading=12)),
                Paragraph(sheet_box_html, ParagraphStyle('SheetNumBox', fontName='Helvetica-Bold', fontSize=22 if (is_overflow_sheet or len(sheet_label_str) <= 3) else (14 if len(sheet_label_str) > 6 else 16), leading=22, alignment=1))
            ]
        ]

        header_table = Table(header_table_data, colWidths=[total_table_width * 0.28, total_table_width * 0.35, total_table_width * 0.22, total_table_width * 0.15])
        header_table.setStyle(TableStyle([
            ('BOX', (0,0), (-1,-1), 2, colors.black),
            ('INNERGRID', (0,0), (-1,-1), 1.5, colors.black),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BACKGROUND', (3,0), (3,0), colors.HexColor('#f3f4f6')),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]))
        sub_story.append(header_table)
        sub_story.append(Spacer(1, 8))

        grid_data = []
        grid_data.append(['S', 'Ring', '1st', '2nd', '', 'Ring', '1st', '2nd', '', 'Ring', '1st', '2nd'])

        rows_per_col = 30 if (is_overflow_sheet or is_filter) else 32
        data_rows = data_subset[:3 * rows_per_col]

        col1_items = data_rows[0:rows_per_col]
        col2_items = data_rows[rows_per_col:2*rows_per_col]
        col3_items = data_rows[2*rows_per_col:3*rows_per_col]

        def process_items(items_list):
            processed = []
            for r in items_list:
                ring = str(r[0]) if r[0] is not None else ""
                q1_val = r[1]
                q2_val = r[2]
                
                q1 = str(q1_val) if q1_val not in ("", None, 0, "0") else ""
                q2 = str(q2_val) if q2_val not in ("", None, 0, "0") else ""
                
                processed.append((ring, q1, q2))
            return processed

        p_col1 = process_items(col1_items)
        p_col2 = process_items(col2_items)
        p_col3 = process_items(col3_items)

        for r in range(rows_per_col):
            s_no = str(r + 1)
            item1 = p_col1[r] if r < len(p_col1) else ("", "", "")
            item2 = p_col2[r] if r < len(p_col2) else ("", "", "")
            item3 = p_col3[r] if r < len(p_col3) else ("", "", "")

            grid_data.append([
                s_no,
                item1[0], item1[1], item1[2],
                "",
                item2[0], item2[1], item2[2],
                "",
                item3[0], item3[1], item3[2]
            ])

        def get_col_totals(items):
            t_q1 = sum(int(r[1]) for r in items if str(r[1]).isdigit())
            t_q2 = sum(int(r[2]) for r in items if str(r[2]).isdigit())
            return t_q1, t_q2

        tot1_q1, tot1_q2 = get_col_totals(col1_items)
        tot2_q1, tot2_q2 = get_col_totals(col2_items)
        tot3_q1, tot3_q2 = get_col_totals(col3_items)

        grid_data.append([
            "TOTAL", "", str(tot1_q1), str(tot1_q2),
            "",
            "TOTAL", str(tot2_q1), str(tot2_q2),
            "",
            "TOTAL", str(tot3_q1), str(tot3_q2)
        ])

        t = Table(grid_data, colWidths=col_widths, rowHeights=[22] + [22]*rows_per_col + [24])
        t.setStyle(TableStyle([
            ('BOX', (0,0), (-1,-1), 2, colors.black),
            ('GRID', (0,0), (-1,-1), 1.5, colors.black),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 11),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#d1d5db')),
            ('SPAN', (0, -1), (1, -1)),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#f3f4f6')),
            ('LINEBEFORE', (4,0), (4,-1), 1.5, colors.black),
            ('LINEAFTER', (4,0), (4,-1), 1.5, colors.black),
            ('LINEBEFORE', (8,0), (8,-1), 1.5, colors.black),
            ('LINEAFTER', (8,0), (8,-1), 1.5, colors.black),
            ('BACKGROUND', (4,0), (4,-1), colors.white),
            ('BACKGROUND', (8,0), (8,-1), colors.white),
        ]))
        sub_story.append(t)
        sub_story.append(Spacer(1, 8))

        grand_tot = tot1_q1 + tot1_q2 + tot2_q1 + tot2_q2 + tot3_q1 + tot3_q2
        footer_data = [[f"GRAND TOTAL: {grand_tot}", "Prize: ______________"]]
        foot_table = Table(footer_data, colWidths=[total_table_width * 0.52, total_table_width * 0.48])
        foot_table.setStyle(TableStyle([
            ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 12),
            ('ALIGN', (0,0), (0,0), 'LEFT'),
            ('ALIGN', (1,0), (1,0), 'RIGHT'),
        ]))
        sub_story.append(foot_table)
        return sub_story

    if sheet_filter in ["overflow", "all_overflow", "bluff"]:
        query = "SELECT ring_number, qty_1st, qty_2nd FROM inventory WHERE draw_date=? AND sheet_type LIKE 'overflow%' ORDER BY id ASC"
        cursor.execute(query, (active_draw,))
        o_rows = cursor.fetchall()

        chunk_size = 90
        total_chunks = max(1, math.ceil(len(o_rows) / chunk_size))

        for c_idx in range(total_chunks):
            chunk_data = o_rows[c_idx * chunk_size : (c_idx + 1) * chunk_size]
            lbl = f"{c_idx + 1}"
            story.extend(build_single_voucher_story(chunk_data, lbl, is_overflow_sheet=True, is_filter=False))

            if c_idx < total_chunks - 1:
                story.append(PageBreak())

    elif sheet_filter in ["filter", "all_filter"]:
        query = "SELECT ring_number, qty_1st, qty_2nd FROM inventory WHERE draw_date=? AND sheet_type LIKE 'filter%' ORDER BY id ASC"
        cursor.execute(query, (active_draw,))
        f_rows = cursor.fetchall()

        chunk_size = 90
        total_chunks = max(1, math.ceil(len(f_rows) / chunk_size))

        for c_idx in range(total_chunks):
            chunk_data = f_rows[c_idx * chunk_size : (c_idx + 1) * chunk_size]
            lbl = f"FILTER ({c_idx+1}/{total_chunks})" if total_chunks > 1 else "FILTER"
            story.extend(build_single_voucher_story(chunk_data, lbl, is_overflow_sheet=False, is_filter=True))

            if c_idx < total_chunks - 1:
                story.append(PageBreak())

    else:
        query = "SELECT ring_number, qty_1st, qty_2nd FROM inventory WHERE draw_date=? AND sheet_type='main'"
        params = [active_draw]
        if target_ring:
            query += " AND ring_number=?"
            params.append(target_ring)
        query += " ORDER BY id ASC"

        cursor.execute(query, params)
        raw_data_rows = cursor.fetchall()

        chunk_size = 96
        total_chunks = max(1, math.ceil(len(raw_data_rows) / chunk_size))

        for c_idx in range(total_chunks):
            chunk_data = raw_data_rows[c_idx * chunk_size : (c_idx + 1) * chunk_size]
            lbl = f"MAIN ({c_idx+1}/{total_chunks})" if total_chunks > 1 else "MAIN"
            story.extend(build_single_voucher_story(chunk_data, lbl, is_overflow_sheet=False, is_filter=False))

            if c_idx < total_chunks - 1:
                story.append(PageBreak())

    conn.close()

    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Prime X7</title>
    <link href="https://fonts.googleapis.com/css2?family=Noto+Nastaliq+Urdu:wght@700&family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-body: #f4f1ea;
            --text-main: #2c302e;
            --header-bg: linear-gradient(180deg, #fcfbf9 0%, #f7f4ed 100%);
            --header-border: #e5e0d3;
            --card-bg: #fdfcf9;
            --card-border: #e5e0d3;
            --card-shadow: rgba(100, 80, 50, 0.04);
            --input-bg: #f7f4ed;
            --input-border: #dcd5c7;
            --table-th-bg: #ede8dc;
            --table-th-text: #92400e;
            --table-row-even: #f8f5ed;
            --table-row-odd: #fdfcf9;
            --table-row-hover: #f3efe4;
            --table-border: #eae5d9;
            --subtext-color: #6e7269;
            --entry-container-bg: linear-gradient(180deg, #fdfcf9 0%, #f7f4ed 100%);
        }

        [data-theme="slate"] {
            --bg-body: #e2e8f0;
            --text-main: #0f172a;
            --header-bg: linear-gradient(180deg, #ffffff 0%, #f1f5f9 100%);
            --header-border: #cbd5e1;
            --card-bg: #ffffff;
            --card-border: #cbd5e1;
            --card-shadow: rgba(15, 23, 42, 0.05);
            --input-bg: #f8fafc;
            --input-border: #94a3b8;
            --table-th-bg: #cbd5e1;
            --table-th-text: #1e293b;
            --table-row-even: #f1f5f9;
            --table-row-odd: #ffffff;
            --table-row-hover: #e2e8f0;
            --table-border: #cbd5e1;
            --subtext-color: #475569;
            --entry-container-bg: linear-gradient(180deg, #ffffff 0%, #f1f5f9 100%);
        }

        [data-theme="glitter"] {
            --bg-body: #0b0f19;
            --text-main: #f8fafc;
            --header-bg: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #0f172a 100%);
            --header-border: #4338ca;
            --card-bg: #111827;
            --card-border: #312e81;
            --card-shadow: rgba(124, 58, 237, 0.25);
            --input-bg: #1e1b4b;
            --input-border: #4f46e5;
            --table-th-bg: #1e1b4b;
            --table-th-text: #f59e0b;
            --table-row-even: #0f172a;
            --table-row-odd: #111827;
            --table-row-hover: #2e1065;
            --table-border: #1f2937;
            --subtext-color: #a5b4fc;
            --entry-container-bg: linear-gradient(180deg, #111827 0%, #0b0f19 100%);
        }

        [data-theme="glitter"] .table-card, 
        [data-theme="glitter"] .menu-card, 
        [data-theme="glitter"] .info-card {
            box-shadow: 0 4px 20px rgba(139, 92, 246, 0.15), 0 0 10px rgba(245, 158, 11, 0.08);
            border: 1px solid #3730a3;
        }

        [data-theme="glitter"] .bill-btn {
            background: linear-gradient(135deg, #1f1937 0%, #111827 100%);
            border-color: #4338ca;
            box-shadow: 0 0 8px rgba(168, 85, 247, 0.2);
        }

        @keyframes headerBlink {
            0% { background: #7f1d1d; border-color: #f87171; box-shadow: 0 0 15px rgba(239, 68, 68, 0.8); }
            50% { background: #450a0a; border-color: #dc2626; box-shadow: 0 0 5px rgba(153, 27, 27, 0.4); }
            100% { background: #7f1d1d; border-color: #f87171; box-shadow: 0 0 15px rgba(239, 68, 68, 0.8); }
        }

        .advance-draw-blink {
            animation: headerBlink 1.2s infinite ease-in-out !important;
        }

        * { box-sizing: border-box; font-family: 'Inter', system-ui, -apple-system, sans-serif; -webkit-touch-callout: none; transition: background-color 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease, color 0.2s ease; }
        html, body { height: 100%; margin: 0; padding: 0; background: var(--bg-body); color: var(--text-main); overflow: hidden; }
        .main-app-container { display: flex; flex-direction: column; height: 100vh; max-height: 100vh; width: 100%; overflow: hidden; }
        
        .fixed-header { flex-shrink: 0; background: var(--header-bg); padding: 8px 12px; box-shadow: 0 4px 15px var(--card-shadow); z-index: 200; border-bottom: 1px solid var(--header-border); }
        
        .static-banner { position: relative; background: linear-gradient(135deg, #1e3a8a 0%, #1d4ed8 100%); color: #ffffff; border: none; padding: 6px; border-radius: 8px; margin-bottom: 8px; text-align: center; box-shadow: 0 3px 10px rgba(29,78,216,0.2); display: flex; align-items: center; justify-content: center; }
        .static-banner-text { font-family: 'Noto Nastaliq Urdu', serif; font-weight: 700; font-size: 1rem; margin: 0; color: #ffffff; text-shadow: 0 1px 2px rgba(0,0,0,0.2); }
        
        .theme-switch-btn { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); background: rgba(255, 255, 255, 0.2); border: 1px solid rgba(255, 255, 255, 0.4); color: #ffffff; border-radius: 20px; padding: 3px 10px; font-size: 0.75rem; font-weight: 800; cursor: pointer; display: flex; align-items: center; gap: 4px; backdrop-filter: blur(4px); box-shadow: 0 2px 4px rgba(0,0,0,0.15); }
        .theme-switch-btn:hover { background: rgba(255, 255, 255, 0.35); transform: translateY(-50%) scale(1.02); }

        .blocks-grid { display: grid; grid-template-columns: 1.2fr 1.8fr 40px; gap: 8px; align-items: center; }
        .info-card { background: var(--card-bg); padding: 5px 8px; border-radius: 10px; border: 1px solid var(--card-border); text-align: center; box-shadow: 0 2px 5px var(--card-shadow); }
        .info-card select { width: 100%; background: var(--input-bg); color: #92400e; border: 1px solid var(--input-border); padding: 6px; border-radius: 6px; font-weight: 800; font-size: 0.85rem; outline: none; cursor: pointer; }
        .info-card select:focus { border-color: #d97706; box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15); }
        
        .bill-flex { display: flex; justify-content: center; gap: 6px; align-items: center; }
        .bill-btn { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 6px 8px; text-decoration: none; text-align: center; cursor: pointer; display: inline-block; min-width: 68px; box-shadow: 0 2px 4px var(--card-shadow); }
        .bill-btn:hover { background: var(--input-bg); transform: translateY(-1px); box-shadow: 0 4px 8px var(--card-shadow); }
        .bill-btn .label { font-size: 0.65rem; font-weight: 800; color: var(--subtext-color); text-transform: uppercase; letter-spacing: 0.5px; display: block; }
        .bill-btn .val { font-size: 1.05rem; font-weight: 900; color: #b45309; display: block; }
        
        .menu-icon-btn { background: var(--card-bg); color: #b45309; border: 1px solid var(--card-border); border-radius: 8px; font-size: 1.25rem; cursor: pointer; height: 40px; width: 40px; display: flex; align-items: center; justify-content: center; box-shadow: 0 2px 4px var(--card-shadow); }
        .menu-icon-btn:hover { background: var(--input-bg); border-color: var(--input-border); }
        
        .alert { background: #fff5f5; color: #991b1b; border: 1px solid #fecaca; padding: 8px; border-radius: 8px; margin: 6px 10px; font-size: 0.9rem; font-weight: 700; text-align: center; flex-shrink: 0; box-shadow: 0 2px 6px rgba(239,68,68,0.08); }
        
        .sms-card { background: var(--card-bg); margin: 6px 10px 0 10px; padding: 6px; border-radius: 12px; border: 1px solid var(--card-border); box-shadow: 0 2px 8px var(--card-shadow); }
        .sms-card textarea { width: 100%; height: 210px; background: var(--input-bg); color: var(--text-main); border: 1px solid var(--input-border); border-radius: 8px; padding: 8px; font-size: 0.85rem; resize: vertical; outline: none; }
        .sms-card textarea:focus { border-color: #d97706; background: #ffffff; box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15); }
        
        .filter-tab-container { display: flex; gap: 6px; padding: 6px 10px 2px 10px; overflow-x: auto; flex-shrink: 0; }
        .filter-tab { flex: 1; min-width: 60px; height: 30px; background: var(--card-bg); color: var(--subtext-color); border: 1px solid var(--card-border); border-radius: 8px; font-size: 0.78rem; font-weight: 800; display: flex; align-items: center; justify-content: center; cursor: pointer; box-shadow: 0 2px 4px var(--card-shadow); }
        .filter-tab.active { background: linear-gradient(135deg, #d97706 0%, #b45309 100%); color: #ffffff; border-color: transparent; box-shadow: 0 3px 8px rgba(217,119,6,0.3); }

        .table-scroll-container { flex: 1 1 auto; overflow-y: auto; padding: 6px 10px 90px 10px; }
        .table-card { background: var(--card-bg); padding: 8px; border-radius: 12px; border: 1px solid var(--card-border); box-shadow: 0 4px 15px var(--card-shadow); }
        table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 1rem; text-align: center; border-radius: 8px; overflow: hidden; }
        th, td { border-bottom: 1px solid var(--table-border); border-right: 1px solid var(--table-border); padding: 8px 6px; font-weight: 700; color: var(--text-main); }
        th:last-child, td:last-child { border-right: none; }
        th { background: var(--table-th-bg); color: var(--table-th-text); font-size: 0.8rem; letter-spacing: 0.5px; text-transform: uppercase; position: sticky; top: 0; z-index: 10; border-bottom: 2px solid var(--input-border); }
        .ring-row { user-select: none; cursor: pointer; }
        .ring-row:hover { background: var(--table-row-hover) !important; }
        .ring-row:nth-child(even) { background: var(--table-row-even); }
        .ring-row:nth-child(odd) { background: var(--table-row-odd); }
        
        .entry-container { position: fixed; bottom: 0; left: 0; right: 0; background: var(--entry-container-bg); padding: 10px 12px; border-top: 1px solid var(--card-border); z-index: 300; box-shadow: 0 -4px 20px var(--card-shadow); }
        .horizontal-form { display: flex; gap: 8px; align-items: flex-start; }
        .field-group { flex: 1; display: flex; flex-direction: column; align-items: center; }
        .field-group input { width: 100%; height: 42px; padding: 4px; background: var(--input-bg); color: var(--text-main); border: 2px solid var(--input-border); border-radius: 8px; text-align: center; font-size: 1.15rem; font-weight: 800; }
        .field-group input:disabled { background: var(--table-border); color: #a0a59c; cursor: not-allowed; border-color: var(--input-border); }
        .field-group input:focus { border-color: #d97706; outline: none; background: #ffffff; box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15); }
        .rem-text { color: #dc2626; font-size: 1.7rem; font-weight: 900; margin-top: 2px; height: 24px; text-align: center; }
        .ok-btn { background: linear-gradient(135deg, #d97706 0%, #b45309 100%); color: #ffffff; border: none; padding: 0 20px; border-radius: 8px; font-weight: 900; font-size: 1.1rem; cursor: pointer; height: 42px; box-shadow: 0 3px 10px rgba(217,119,6,0.3); }
        .ok-btn:disabled { background: var(--input-border); color: #9ca3af; cursor: not-allowed; box-shadow: none; }
        .ok-btn:hover:not(:disabled) { background: linear-gradient(135deg, #b45309 0%, #92400e 100%); transform: translateY(-1px); }

        .page-section { display: none; height: 100%; overflow-y: auto; background: var(--bg-body); }
        .page-section.active { display: block; }
        .menu-padding { padding: 14px; }
        .menu-header { display: flex; justify-content: space-between; align-items: center; background: var(--card-bg); padding: 12px 14px; border-radius: 10px; border: 1px solid var(--card-border); margin-bottom: 12px; box-shadow: 0 2px 6px var(--card-shadow); }
        .menu-card { background: var(--card-bg); padding: 14px; border-radius: 10px; border: 1px solid var(--card-border); margin-bottom: 12px; color: var(--text-main); box-shadow: 0 2px 6px var(--card-shadow); }
        .menu-card h3 { color: #b45309; margin-top: 0; font-size: 1rem; border-bottom: 2px solid var(--table-border); padding-bottom: 6px; font-weight: 800; }
        .limit-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
        .limit-grid label { font-size: 0.75rem; font-weight: 800; color: var(--subtext-color); }
        .limit-grid input { width: 100%; height: 38px; padding: 6px; background: var(--input-bg); color: var(--text-main); border: 1px solid var(--input-border); border-radius: 6px; font-weight: bold; }
        .limit-grid input:focus { border-color: #d97706; outline: none; background: #ffffff; box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15); }
        .btn { width: 100%; padding: 10px; border: none; border-radius: 8px; font-weight: 800; cursor: pointer; text-align: center; text-decoration: none; display: block; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
        .btn:active { transform: scale(0.98); }
        .btn-green { background: linear-gradient(135deg, #16a34a 0%, #15803d 100%); color: white; }
        .btn-green:hover { background: linear-gradient(135deg, #15803d 0%, #166534 100%); }
        .btn-dark { background: linear-gradient(135deg, #475569 0%, #334155 100%); color: white; }
        .btn-dark:hover { background: linear-gradient(135deg, #334155 0%, #1e293b 100%); }
        .btn-blue { background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); color: white; }
        .btn-blue:hover { background: linear-gradient(135deg, #1d4ed8 0%, #1e40af 100%); }
        .btn-red { background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); color: white; }
        .btn-red:hover { background: linear-gradient(135deg, #b91c1c 0%, #991b1b 100%); }
    </style>
</head>
<body data-theme="warm">

    <div id="mainPage" class="page-section {% if not show_menu %}active{% endif %}">
        <div class="main-app-container">
            
            <div class="fixed-header {% if is_advance %}advance-draw-blink{% endif %}">
                <div class="static-banner">
                    <div class="static-banner-text">بِسْمِ اللّٰهِ الرَّحْمٰنِ الرَّحِیْمِ</div>
                    <button type="button" class="theme-switch-btn" onclick="toggleTheme()" title="Switch Theme Mode">
                        <span id="themeIcon">🌾</span> <span id="themeLabel">Warm</span>
                    </button>
                </div>

                <div class="blocks-grid">
                    <div class="info-card">
                        {% if draw_files %}
                        <form id="switchDrawForm" action="{{ url_for('select_draw') }}" method="post">
                            <select name="active_draw" onchange="document.getElementById('switchDrawForm').submit();">
                                {% for file in draw_files %}
                                <option value="{{ file[0] }}" {% if file[0] == active_draw %}selected{% endif %}>
                                    {{ file[0] }} - {{ file[1] }}
                                </option>
                                {% endfor %}
                            </select>
                        </form>
                        {% else %}
                        <div style="font-size:0.75rem; color:#dc2626; font-weight: bold;">No File</div>
                        {% endif %}
                    </div>
                    
                    <div class="info-card">
                        <div class="bill-flex">
                            <a href="{{ url_for('index', source='main', search_ring=searched_ring) }}" class="bill-btn">
                                <span class="label">Main</span>
                                <span class="val" id="mainSaleVal">{{ sales.main }}</span>
                            </a>
                            <a href="{{ url_for('index', source='overflow', search_ring=searched_ring) }}" class="bill-btn">
                                <span class="label">Bluff</span>
                                <span class="val" id="overflowSaleVal" style="color:#d97706;">{{ sales.overflow }}</span>
                            </a>
                            <a href="{{ url_for('index', source='filter', search_ring=searched_ring) }}" class="bill-btn">
                                <span class="label">Filter</span>
                                <span class="val" id="filterSaleVal" style="color:#2563eb;">{{ sales.filter }}</span>
                            </a>
                        </div>
                    </div>

                    <button class="menu-icon-btn" onclick="openMenu()">&#9776;</button>
                </div>
            </div>

            <div id="alertBox" class="alert" style="display: {% if flash_err %}block{% else %}none{% endif %};">
                <span id="alertText">{{ flash_err }}</span>
            </div>

            {% if sms_enabled %}
            <div class="sms-card">
                <form action="{{ url_for('process_sms') }}" method="post">
                    <input type="hidden" name="active_draw" value="{{ active_draw }}">
                    <div style="display:flex; gap:6px; align-items:center;">
                        <textarea name="sms_text" placeholder="Paste SMS e.g. 332=25/25, XX65=50/50, X657=100/100, X2XX=50"></textarea>
                        <button type="submit" class="btn btn-green" style="width:70px; height:210px; font-size:0.85rem;">Add SMS</button>
                    </div>
                </form>
            </div>
            {% endif %}

            <div class="filter-tab-container">
                <div class="filter-tab active" onclick="filterDigits('all', this)">All</div>
                <div class="filter-tab" onclick="filterDigits('1', this)">فگر</div>
                <div class="filter-tab" onclick="filterDigits('2', this)">آکڑہ</div>
                <div class="filter-tab" onclick="filterDigits('3', this)">ٹنڈولہ</div>
                <div class="filter-tab" onclick="filterDigits('4', this)">پنگوڑا</div>
            </div>

            <div class="table-scroll-container">
                <div class="table-card">
                    <table id="inventoryTable">
                        <thead>
                            <tr>
                                <th>Ring</th>
                                <th>1ST</th>
                                <th>2ND</th>
                            </tr>
                        </thead>
                        <tbody id="tableBody">
                            {% for row in rows %}
                            <tr class="ring-row" data-id="{{ row[0] }}" data-digits="{{ row[2]|length }}">
                                <td>{{ row[2] }}</td>
                                <td>{% if row[3] and row[3] != 0 %}{{ row[3] }}{% endif %}</td>
                                <td>{% if row[4] and row[4] != 0 %}{{ row[4] }}{% endif %}</td>
                            </tr>
                            {% else %}
                            <tr id="noEntriesRow"><td colspan="3" style="color:#64748b;">No entries found. (Long press any row to delete)</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="entry-container">
                <form id="manualEntryForm" onsubmit="submitManualEntry(event)" class="horizontal-form">
                    <input type="hidden" name="active_draw" value="{{ active_draw }}">
                    <div class="field-group">
                        <input type="text" id="ringInput" name="ring" placeholder="Ring" value="{{ searched_ring }}" maxlength="4" inputmode="numeric" oninput="onRingInputChanged()" required autocomplete="off">
                        <div class="rem-text"></div>
                    </div>
                    <div class="field-group">
                        <input type="number" id="q1Input" name="q1" placeholder="1ST" disabled autocomplete="off">
                        <div class="rem-text" id="rem1stText">{% if rem_1st is not none %}{{ rem_1st }}{% endif %}</div>
                    </div>
                    <div class="field-group">
                        <input type="number" id="q2Input" name="q2" placeholder="2ND" disabled autocomplete="off">
                        <div class="rem-text" id="rem2ndText">{% if rem_2nd is not none %}{{ rem_2nd }}{% endif %}</div>
                    </div>
                    <button type="submit" id="okBtn" class="ok-btn" disabled>OK</button>
                </form>
            </div>
        </div>
    </div>

    <div id="menuPage" class="page-section {% if show_menu %}active{% endif %}">
        <div class="menu-padding">
            <div class="menu-header">
                <button class="ok-btn" style="height:32px; font-size:0.8rem;" onclick="closeMenu()">← Back</button>
                <h2 style="margin:0; color:#b45309; font-size: 1.1rem;">Menu Options</h2>
            </div>

            <!-- 1. SMS Enable/Disable button at the top -->
            <div class="menu-card">
                <h3>SMS Paste Box Setting</h3>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:0.85rem; font-weight:bold; color:var(--text-main);">
                        Status: <b>{% if sms_enabled %}ENABLED{% else %}DISABLED{% endif %}</b>
                    </span>
                    <form action="{{ url_for('toggle_sms_setting') }}" method="post" style="margin:0;">
                        {% if sms_enabled %}
                        <button type="submit" class="btn btn-red" style="width:110px; padding:6px; font-size:0.8rem;">Disable SMS</button>
                        {% else %}
                        <button type="submit" class="btn btn-green" style="width:110px; padding:6px; font-size:0.8rem;">Enable SMS</button>
                        {% endif %}
                    </form>
                </div>
            </div>

            <!-- 2. Ring search for Main file -->
            <div class="menu-card">
                <h3>Search MAIN File</h3>
                <div style="display:flex; gap:6px;">
                    <input type="text" id="menuSearchMainInput" placeholder="Enter Ring Number" maxlength="4" style="flex:1; height:36px; border:1px solid var(--input-border); background:var(--input-bg); color:var(--text-main); border-radius:6px; padding:6px; font-weight:bold;">
                    <button type="button" class="btn btn-dark" style="width:100px; height:36px; padding:0;" onclick="searchInMenu('main', 'menuSearchMainInput', 'menuSearchMainResults')">SEARCH</button>
                </div>
                <div id="menuSearchMainResults"></div>
            </div>

            <!-- 3. Ring search for bluff sheets -->
            <div class="menu-card">
                <h3>Search BLUFF Sheets</h3>
                <div style="display:flex; gap:6px;">
                    <input type="text" id="menuSearchBluffInput" placeholder="Enter Ring Number" maxlength="4" style="flex:1; height:36px; border:1px solid var(--input-border); background:var(--input-bg); color:var(--text-main); border-radius:6px; padding:6px; font-weight:bold;">
                    <button type="button" class="btn btn-dark" style="width:100px; height:36px; padding:0;" onclick="searchInMenu('overflow', 'menuSearchBluffInput', 'menuSearchBluffResults')">SEARCH</button>
                </div>
                <div id="menuSearchBluffResults"></div>
            </div>

            <!-- 4. Download all main file -->
            <div class="menu-card">
                <a href="{{ url_for('download_pdf', sheet='main') }}" class="btn btn-blue">Main Vouchers Download</a>
            </div>

            <!-- 5. Download all bluff sheets -->
            <div class="menu-card">
                <a href="{{ url_for('download_pdf', sheet='overflow') }}" class="btn btn-green">Bluff Sheets Download</a>
            </div>

            <!-- 6. Download all filter sheets (Red Color) -->
            <div class="menu-card">
                <a href="{{ url_for('download_pdf', sheet='filter') }}" class="btn btn-red">Filter Sheets Download</a>
            </div>

            <!-- 7. MAIN File Limits -->
            <div class="menu-card">
                <h3>MAIN File Limits</h3>
                <form action="{{ url_for('update_limits') }}" method="post">
                    <div class="limit-grid">
                        <div><label>1D 1ST (فگر):</label><input type="number" name="exp_limit_1st_1d" value="{{ limits.get('exp_limit_1st_1d', 700) }}"></div>
                        <div><label>1D 2ND (فگر):</label><input type="number" name="exp_limit_2nd_1d" value="{{ limits.get('exp_limit_2nd_1d', 1000) }}"></div>
                        <div><label>2D 1ST (آکڑہ):</label><input type="number" name="exp_limit_1st_2d" value="{{ limits.get('exp_limit_1st_2d', 250) }}"></div>
                        <div><label>2D 2ND (آکڑہ):</label><input type="number" name="exp_limit_2nd_2d" value="{{ limits.get('exp_limit_2nd_2d', 500) }}"></div>
                        <div><label>3D 1ST (ٹنڈولہ):</label><input type="number" name="exp_limit_1st_3d" value="{{ limits.get('exp_limit_1st_3d', 50) }}"></div>
                        <div><label>3D 2ND (ٹنڈولہ):</label><input type="number" name="exp_limit_2nd_3d" value="{{ limits.get('exp_limit_2nd_3d', 50) }}"></div>
                        <div><label>4D 1ST (پنگوڑا):</label><input type="number" name="exp_limit_1st_4d" value="{{ limits.get('exp_limit_1st_4d', 25) }}"></div>
                        <div><label>4D 2ND (پنگوڑا):</label><input type="number" name="exp_limit_2nd_4d" value="{{ limits.get('exp_limit_2nd_4d', 25) }}"></div>
                    </div>
                    <button type="submit" class="btn btn-dark">SAVE MAIN LIMITS</button>
                </form>
            </div>

            <!-- 8. BLUFF Sheets Limits -->
            <div class="menu-card">
                <h3>BLUFF Sheets Limits</h3>
                <form action="{{ url_for('update_limits') }}" method="post">
                    <div class="limit-grid">
                        <div><label>1D 1ST (فگر):</label><input type="number" name="imp_limit_1st_1d" value="{{ limits.get('imp_limit_1st_1d', 2000) }}"></div>
                        <div><label>1D 2ND (فگر):</label><input type="number" name="imp_limit_2nd_1d" value="{{ limits.get('imp_limit_2nd_1d', 3000) }}"></div>
                        <div><label>2D 1ST (آکڑہ):</label><input type="number" name="imp_limit_1st_2d" value="{{ limits.get('imp_limit_1st_2d', 1000) }}"></div>
                        <div><label>2D 2ND (آکڑہ):</label><input type="number" name="imp_limit_2nd_2d" value="{{ limits.get('imp_limit_2nd_2d', 1500) }}"></div>
                        <div><label>3D 1ST (ٹنڈولہ):</label><input type="number" name="imp_limit_1st_3d" value="{{ limits.get('imp_limit_1st_3d', 200) }}"></div>
                        <div><label>3D 2ND (ٹنڈولہ):</label><input type="number" name="imp_limit_2nd_3d" value="{{ limits.get('imp_limit_2nd_3d', 600) }}"></div>
                        <div><label>4D 1ST (پنگوڑا):</label><input type="number" name="imp_limit_1st_4d" value="{{ limits.get('imp_limit_1st_4d', 100) }}"></div>
                        <div><label>4D 2ND (پنگوڑا):</label><input type="number" name="imp_limit_2nd_4d" value="{{ limits.get('imp_limit_2nd_4d', 100) }}"></div>
                    </div>
                    <button type="submit" class="btn btn-dark">SAVE BLUFF LIMITS</button>
                </form>
            </div>

            <!-- File Creation and Controls -->
            <div class="menu-card">
                <h3>Create / Manage Draw Files</h3>
                <form action="{{ url_for('create_draw_file') }}" method="post" style="margin-bottom:12px;">
                    <input type="text" name="new_draw_date" placeholder="Draw Date (DD/MM/YYYY)" required style="width:100%; height:36px; margin-bottom:6px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <input type="text" name="bond_name" placeholder="Bond Name" style="width:100%; height:36px; margin-bottom:6px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <input type="text" name="party_name" placeholder="Party Name" style="width:100%; height:36px; margin-bottom:6px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <input type="text" name="party_code" placeholder="Party Code" style="width:100%; height:36px; margin-bottom:6px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <input type="text" name="city" placeholder="City" style="width:100%; height:36px; margin-bottom:6px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <input type="datetime-local" name="expire_time" placeholder="Closing Time" style="width:100%; height:36px; margin-bottom:8px; background:var(--input-bg); color:var(--text-main); border:1px solid var(--input-border); border-radius:6px; padding:6px;">
                    <button type="submit" class="btn btn-green">CREATE / UPDATE FILE</button>
                </form>

                <h4 style="margin:8px 0 6px 0; font-size:0.85rem; color:var(--subtext-color);">Existing Files Control & Time Editing:</h4>
                <div style="max-height:220px; overflow-y:auto;">
                    {% for f in all_draw_files %}
                    <div style="border-bottom:1px solid var(--table-border); padding:6px 0; font-size:0.8rem;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                            <span style="color:var(--text-main);"><b>{{ f[0] }}</b> ({{ f[1] }})</span>
                            <div style="display:flex; gap:3px;">
                                <a href="{{ url_for('toggle_file_attr', draw=f[0], attr='hide') }}" class="btn btn-dark" style="padding:3px 8px; font-size:0.7rem; width:auto;">
                                    {% if f[5] == 1 %}Unhide{% else %}Hide{% endif %}
                                </a>
                                <a href="{{ url_for('toggle_file_attr', draw=f[0], attr='close') }}" class="btn btn-green" style="padding:3px 8px; font-size:0.7rem; width:auto; background: {% if f[6] == 1 %}#dc2626{% else %}#16a34a{% endif %};">
                                    {% if f[6] == 1 %}Open{% else %}Close{% endif %}
                                </a>
                                <a href="{{ url_for('delete_draw_file', draw=f[0]) }}" class="btn btn-red" style="padding:3px 8px; font-size:0.7rem; width:auto;">Delete</a>
                            </div>
                        </div>
                        <form action="{{ url_for('update_file_time') }}" method="post" style="display:flex; gap:4px; align-items:center;">
                            <input type="hidden" name="draw" value="{{ f[0] }}">
                            <input type="datetime-local" name="expire_time" value="{{ f[7].replace(' ', 'T') if f[7] else '' }}" style="flex:1; height:30px; font-size:0.75rem; padding:2px 6px; border:1px solid var(--input-border); background:var(--input-bg); color:var(--text-main); border-radius:4px;">
                            <button type="submit" class="btn btn-dark" style="padding:2px 8px; font-size:0.7rem; width:auto; height:30px;">Set Time</button>
                        </form>
                    </div>
                    {% endfor %}
                </div>
            </div>

        </div>
    </div>

    <script>
        function applyTheme(theme) {
            document.body.setAttribute('data-theme', theme);
            const icon = document.getElementById('themeIcon');
            const label = document.getElementById('themeLabel');
            if (theme === 'slate') {
                if (icon) icon.innerText = '⚙️';
                if (label) label.innerText = 'Slate';
            } else if (theme === 'glitter') {
                if (icon) icon.innerText = '✨';
                if (label) label.innerText = 'Glitter Dark';
            } else {
                if (icon) icon.innerText = '🌾';
                if (label) label.innerText = 'Warm';
            }
            localStorage.setItem('primeX7Theme', theme);
        }

        function toggleTheme() {
            const currentTheme = document.body.getAttribute('data-theme') || 'warm';
            let nextTheme = 'slate';
            if (currentTheme === 'warm') nextTheme = 'slate';
            else if (currentTheme === 'slate') nextTheme = 'glitter';
            else nextTheme = 'warm';
            applyTheme(nextTheme);
        }

        function initTheme() {
            const savedTheme = localStorage.getItem('primeX7Theme') || 'warm';
            applyTheme(savedTheme);
        }

        function openMenu() {
            document.getElementById('mainPage').classList.remove('active');
            document.getElementById('menuPage').classList.add('active');
        }
        function closeMenu() {
            document.getElementById('menuPage').classList.remove('active');
            document.getElementById('mainPage').classList.add('active');
        }
        function filterDigits(digitType, elem) {
            document.querySelectorAll('.filter-tab').forEach(tab => tab.classList.remove('active'));
            elem.classList.add('active');
            document.querySelectorAll('.ring-row').forEach(row => {
                if (digitType === 'all' || row.getAttribute('data-digits') === digitType) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            });
        }

        async function searchInMenu(source, inputId, containerId) {
            const ring = document.getElementById(inputId).value.trim();
            const container = document.getElementById(containerId);
            const activeDraw = "{{ active_draw }}";
            if (!ring) {
                container.innerHTML = '<div style="font-size:0.8rem; color:var(--subtext-color); margin-top:6px;">Please enter a ring number.</div>';
                return;
            }
            try {
                const res = await fetch(`/api/search_inventory?draw=${activeDraw}&ring=${encodeURIComponent(ring)}&source=${source}`);
                const data = await res.json();
                if (data.status === 'success') {
                    if (data.rows.length === 0) {
                        container.innerHTML = '<div style="font-size:0.8rem; color:#dc2626; margin-top:8px; font-weight:bold;">No matching records found.</div>';
                    } else {
                        let html = `<div style="margin-top:10px; max-height:200px; overflow-y:auto; border:1px solid var(--card-border); border-radius:6px;">
                            <table style="width:100%; font-size:0.85rem; border-collapse:collapse;">
                                <thead>
                                    <tr style="background:var(--table-th-bg); color:var(--table-th-text);">
                                        <th style="padding:4px;">Ring</th>
                                        <th style="padding:4px;">1ST</th>
                                        <th style="padding:4px;">2ND</th>
                                    </tr>
                                </thead>
                                <tbody>`;
                        data.rows.forEach(r => {
                            html += `<tr style="border-bottom:1px solid var(--table-border); text-align:center;">
                                <td style="padding:4px; font-weight:bold;">${r.ring}</td>
                                <td style="padding:4px;">${r.q1 || ''}</td>
                                <td style="padding:4px;">${r.q2 || ''}</td>
                            </tr>`;
                        });
                        html += `</tbody></table></div>`;
                        container.innerHTML = html;
                    }
                } else {
                    container.innerHTML = '<div style="font-size:0.8rem; color:#dc2626; margin-top:6px;">Error searching records.</div>';
                }
            } catch (e) {
                console.error(e);
                container.innerHTML = '<div style="font-size:0.8rem; color:#dc2626; margin-top:6px;">Error searching records.</div>';
            }
        }

        function onRingInputChanged() {
            const ringInput = document.getElementById('ringInput');
            const q1Input = document.getElementById('q1Input');
            const q2Input = document.getElementById('q2Input');
            const okBtn = document.getElementById('okBtn');

            let val = ringInput.value.toUpperCase().replace(/[^0-9X]/g, '');
            if (val.length > 4) val = val.substring(0, 4);
            ringInput.value = val;

            const isValid = val.length >= 1 && val.length <= 4 && /[0-9]/.test(val);

            if (isValid) {
                q1Input.disabled = false;
                q2Input.disabled = false;
                okBtn.disabled = false;
                checkCapacity();
            } else {
                q1Input.disabled = true;
                q2Input.disabled = true;
                okBtn.disabled = true;
                q1Input.value = '';
                q2Input.value = '';
                document.getElementById('rem1stText').innerText = '';
                document.getElementById('rem2ndText').innerText = '';
            }
        }

        async function checkCapacity() {
            const ringVal = document.getElementById('ringInput').value;
            const activeDraw = "{{ active_draw }}";
            if (!ringVal || !activeDraw) {
                document.getElementById('rem1stText').innerText = '';
                document.getElementById('rem2ndText').innerText = '';
                return;
            }
            try {
                const res = await fetch(`/api/check_capacity?draw=${activeDraw}&ring=${ringVal}`);
                const data = await res.json();
                if (data.status === 'ok') {
                    document.getElementById('rem1stText').innerText = `${data.rem_1st}`;
                    document.getElementById('rem2ndText').innerText = `${data.rem_2nd}`;
                } else {
                    document.getElementById('rem1stText').innerText = '';
                    document.getElementById('rem2ndText').innerText = '';
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function submitManualEntry(event) {
            event.preventDefault();
            const ringInput = document.getElementById('ringInput');
            const q1Input = document.getElementById('q1Input');
            const q2Input = document.getElementById('q2Input');

            if (q1Input.disabled || q2Input.disabled || !ringInput.value) {
                return;
            }

            const form = document.getElementById('manualEntryForm');
            const formData = new FormData(form);

            try {
                const response = await fetch('/api/add_manual_ajax', {
                    method: 'POST',
                    body: formData
                });
                const result = await response.json();

                const alertBox = document.getElementById('alertBox');
                const alertText = document.getElementById('alertText');

                if (result.status === 'success') {
                    alertBox.style.display = 'none';
                    alertText.innerText = '';

                    document.getElementById('mainSaleVal').innerText = result.sales.main;
                    document.getElementById('overflowSaleVal').innerText = result.sales.overflow;
                    document.getElementById('filterSaleVal').innerText = result.sales.filter;

                    const tableBody = document.getElementById('tableBody');
                    const noEntries = document.getElementById('noEntriesRow');
                    if (noEntries) {
                        noEntries.remove();
                    }

                    const newRow = document.createElement('tr');
                    newRow.className = 'ring-row';
                    newRow.setAttribute('data-id', result.row.id);
                    newRow.setAttribute('data-digits', result.row.ring.length);
                    newRow.innerHTML = `
                        <td>${result.row.ring}</td>
                        <td>${result.row.q1 !== 0 ? result.row.q1 : ''}</td>
                        <td>${result.row.q2 !== 0 ? result.row.q2 : ''}</td>
                    `;
                    bindLongPress(newRow);
                    tableBody.insertBefore(newRow, tableBody.firstChild);

                    ringInput.value = '';
                    q1Input.value = '';
                    q2Input.value = '';
                    document.getElementById('rem1stText').innerText = '';
                    document.getElementById('rem2ndText').innerText = '';

                    onRingInputChanged();
                    ringInput.focus();
                } else {
                    alertBox.style.display = 'block';
                    alertText.innerText = result.message;
                    onRingInputChanged();
                }
            } catch (e) {
                console.error(e);
                onRingInputChanged();
            }
        }

        function bindLongPress(row) {
            let pressTimer;
            const recordId = row.getAttribute('data-id');

            const startPress = (e) => {
                pressTimer = setTimeout(() => {
                    if (confirm("Are you sure you want to delete this entry?")) {
                        deleteEntry(recordId, row);
                    }
                }, 750);
            };

            const cancelPress = (e) => {
                clearTimeout(pressTimer);
            };

            row.addEventListener('touchstart', startPress);
            row.addEventListener('touchend', cancelPress);
            row.addEventListener('touchmove', cancelPress);
            row.addEventListener('mousedown', startPress);
            row.addEventListener('mouseup', cancelPress);
            row.addEventListener('mouseleave', cancelPress);
        }

        async function deleteEntry(id, rowElement) {
            try {
                const res = await fetch(`/api/delete_entry?id=${id}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    rowElement.remove();
                    document.getElementById('mainSaleVal').innerText = data.sales.main;
                    document.getElementById('overflowSaleVal').innerText = data.sales.overflow;
                    document.getElementById('filterSaleVal').innerText = data.sales.filter;
                } else {
                    alert(data.message || 'Failed to delete');
                }
            } catch (e) {
                console.error(e);
            }
        }

        document.addEventListener("DOMContentLoaded", function() {
            initTheme();
            onRingInputChanged();
            document.addEventListener('click', function(event) {
                const entryContainer = document.querySelector('.entry-container');
                const activeInputs = [document.getElementById('ringInput'), document.getElementById('q1Input'), document.getElementById('q2Input')];
                
                if (entryContainer && !entryContainer.contains(event.target)) {
                    activeInputs.forEach(input => {
                        if (input) input.blur();
                    });
                }
            });

            document.querySelectorAll('.ring-row').forEach(row => bindLongPress(row));

            // Real-time synchronization check every 2 seconds across multiple mobile phones
            setInterval(function() {
                const activeElement = document.activeElement;
                // Do not auto-sync/reload if user is currently typing in an input or textarea
                if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA')) {
                    return;
                }
                // Optional soft background sync or light reload to keep multiple phones updated instantly
                // window.location.reload(); 
            }, 2000);
        });
    </script>
</body>
</html>
'''

@app.route("/")
def index():
    global CURRENT_DRAW_DATE
    init_db()
    limits = get_limits()
    draw_files = get_visible_draw_files()
    all_draw_files = get_all_draw_files()
    
    if draw_files and CURRENT_DRAW_DATE not in [f[0] for f in draw_files]:
        CURRENT_DRAW_DATE = draw_files[0][0]
        
    active_draw = CURRENT_DRAW_DATE
    sales = get_total_sales_breakdown(active_draw)
    is_advance = is_advance_draw(active_draw)
    
    searched_ring = request.args.get("search_ring", "")
    source_filter = request.args.get("source", "main")
    sms_enabled = str(limits.get("sms_paste_enabled", "1")) == "1"

    raw_rows = []
    if active_draw:
        conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
        cursor = conn.cursor()

        query = "SELECT id, draw_date, ring_number, qty_1st, qty_2nd, sheet_type FROM inventory WHERE draw_date=?"
        params = [active_draw]

        if source_filter == "main":
            query += " AND sheet_type='main'"
        elif source_filter == "overflow":
            query += " AND sheet_type LIKE 'overflow%'"
        elif source_filter == "filter":
            query += " AND sheet_type LIKE 'filter%'"

        if searched_ring:
            query += " AND ring_number=?"
            params.append(searched_ring)

        query += " ORDER BY id DESC"
        cursor.execute(query, params)
        raw_rows = cursor.fetchall()
        conn.close()

    rem_1st, rem_2nd = get_remaining_capacity(active_draw, searched_ring, limits)
    flash_err = request.args.get("err", "")

    return render_template_string(
        HTML_TEMPLATE,
        rows=raw_rows,
        limits=limits,
        sales=sales,
        draw_files=draw_files,
        all_draw_files=all_draw_files,
        active_draw=active_draw,
        is_advance=is_advance,
        searched_ring=searched_ring,
        rem_1st=rem_1st,
        rem_2nd=rem_2nd,
        flash_err=flash_err,
        sms_enabled=sms_enabled,
        show_menu=False
    )

@app.route("/api/check_capacity")
def api_check_capacity():
    draw = request.args.get("draw", "")
    ring = request.args.get("ring", "")
    limits = get_limits()
    rem_1st, rem_2nd = get_remaining_capacity(draw, ring, limits)
    if rem_1st is not None:
        return jsonify({"status": "ok", "rem_1st": rem_1st, "rem_2nd": rem_2nd})
    return jsonify({"status": "error"})

@app.route("/api/search_inventory")
def api_search_inventory():
    draw = request.args.get("draw", "").strip()
    ring = request.args.get("ring", "").strip().upper()
    source = request.args.get("source", "main").strip()

    if not draw or not ring:
        return jsonify({"status": "error", "message": "Missing parameters"})

    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()

    query = "SELECT id, ring_number, qty_1st, qty_2nd, sheet_type FROM inventory WHERE draw_date=? AND ring_number=?"
    params = [draw, ring]

    if source == "main":
        query += " AND sheet_type='main'"
    elif source == "overflow":
        query += " AND sheet_type LIKE 'overflow%'"
    elif source == "filter":
        query += " AND sheet_type LIKE 'filter%'"

    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    res_list = []
    for r in rows:
        res_list.append({
            "id": r[0],
            "ring": r[1],
            "q1": r[2] if r[2] != 0 else "",
            "q2": r[3] if r[3] != 0 else "",
            "sheet_type": r[4]
        })

    return jsonify({"status": "success", "rows": res_list})

@app.route("/api/delete_entry", methods=["POST"])
def api_delete_entry():
    entry_id = request.args.get("id")
    if not entry_id:
        return jsonify({"status": "error", "message": "Invalid ID"})
    
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT draw_date FROM inventory WHERE id=?", (entry_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "Entry not found"})
    draw_date = row[0]
    
    cursor.execute("DELETE FROM inventory WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    
    sales = get_total_sales_breakdown(draw_date)
    return jsonify({"status": "success", "sales": sales})

@app.route("/select_draw", methods=["POST"])
def select_draw():
    global CURRENT_DRAW_DATE
    CURRENT_DRAW_DATE = request.form.get("active_draw", "")
    return redirect(url_for('index'))

@app.route("/toggle_sms_setting", methods=["POST"])
def toggle_sms_setting():
    limits = get_limits()
    current = str(limits.get("sms_paste_enabled", "1"))
    new_val = "0" if current == "1" else "1"
    
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sms_paste_enabled', ?)", (new_val,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route("/process_sms", methods=["POST"])
def process_sms():
    active_draw = request.form.get("active_draw", "").strip()
    sms_text = request.form.get("sms_text", "").strip()

    if not active_draw:
        return redirect(url_for('index', err="No active file selected. Entry rejected."))

    closed, close_msg = check_file_is_closed(active_draw)
    if closed:
        return redirect(url_for('index', err=close_msg))

    parsed_entries = parse_sms_text(sms_text)
    if not parsed_entries:
        return redirect(url_for('index', err="Invalid SMS format."))

    limits = get_limits()
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()

    err_msg = ""
    for ring, q1, q2 in parsed_entries:
        success, msg = process_single_entry(cursor, active_draw, ring, q1, q2, limits)
        if not success:
            err_msg = msg
            break

    if err_msg:
        conn.rollback()
        conn.close()
        return redirect(url_for('index', err=err_msg))

    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route("/api/add_manual_ajax", methods=["POST"])
def add_manual_ajax():
    active_draw = request.form.get("active_draw", "").strip()
    ring = request.form.get("ring", "").strip()
    q1 = int(request.form.get("q1", 0) or 0)
    q2 = int(request.form.get("q2", 0) or 0)

    if not active_draw:
        return jsonify({"status": "error", "message": "No active file selected. Entry rejected."})

    closed, close_msg = check_file_is_closed(active_draw)
    if closed:
        return jsonify({"status": "error", "message": close_msg})

    limits = get_limits()
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()

    success, msg = process_single_entry(cursor, active_draw, ring, q1, q2, limits)
    if not success:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": msg})

    cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='inventory'")
    seq_row = cursor.fetchone()
    new_id = seq_row[0] if seq_row else 0

    conn.commit()
    conn.close()

    sales = get_total_sales_breakdown(active_draw)
    return jsonify({
        "status": "success",
        "sales": sales,
        "row": {
            "id": new_id,
            "ring": ring,
            "q1": q1,
            "q2": q2
        }
    })

@app.route("/search_ring", methods=["POST"])
def search_ring():
    search_r = request.form.get("search_ring", "").strip()
    source = request.form.get("source", "main")
    return redirect(url_for('index', search_ring=search_r, source=source))

@app.route("/download_pdf")
def download_pdf():
    global CURRENT_DRAW_DATE
    sheet = request.args.get("sheet", "main")
    target_ring = request.args.get("ring", "")
    pdf_buffer = generate_pdf_report(CURRENT_DRAW_DATE, sheet_filter=sheet, target_ring=target_ring)
    filename = f"{CURRENT_DRAW_DATE}_{sheet}.pdf"
    return Response(pdf_buffer, mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename={filename}'})

@app.route("/download_overflow_pdf")
def download_overflow_pdf():
    return redirect(url_for('download_pdf', sheet='overflow'))

@app.route("/download_filter_pdf")
def download_filter_pdf():
    return redirect(url_for('download_pdf', sheet='filter'))

@app.route("/create_draw_file", methods=["POST"])
def create_draw_file():
    global CURRENT_DRAW_DATE
    new_date = request.form.get("new_draw_date", "").strip()
    bond = request.form.get("bond_name", "").strip()
    party = request.form.get("party_name", "").strip()
    code = request.form.get("party_code", "").strip()
    city = request.form.get("city", "").strip()
    expire = request.form.get("expire_time", "").strip()

    if expire:
        expire = expire.replace("T", " ")

    if new_date:
        conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
        cursor = conn.cursor()
        cursor.execute('''INSERT OR REPLACE INTO draw_files 
            (draw_date, bond_name, party_name, party_code, city, expire_time) 
            VALUES (?, ?, ?, ?, ?, ?)''', (new_date, bond, party, code, city, expire))
        conn.commit()
        conn.close()
        CURRENT_DRAW_DATE = new_date
    return redirect(url_for('index'))

@app.route("/update_file_time", methods=["POST"])
def update_file_time():
    draw = request.form.get("draw", "").strip()
    expire = request.form.get("expire_time", "").strip()
    if expire:
        expire = expire.replace("T", " ")
    if draw:
        conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("UPDATE draw_files SET expire_time=? WHERE draw_date=?", (expire, draw))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route("/toggle_file_attr")
def toggle_file_attr():
    draw = request.args.get("draw", "")
    attr = request.args.get("attr", "")
    if draw and attr in ["hide", "close"]:
        conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
        cursor = conn.cursor()
        if attr == "hide":
            cursor.execute("UPDATE draw_files SET is_hidden = CASE WHEN is_hidden=1 THEN 0 ELSE 1 END WHERE draw_date=?", (draw,))
        elif attr == "close":
            cursor.execute("UPDATE draw_files SET is_closed = CASE WHEN is_closed=1 THEN 0 ELSE 1 END WHERE draw_date=?", (draw,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route("/delete_draw_file")
def delete_draw_file():
    global CURRENT_DRAW_DATE
    draw = request.args.get("draw", "")
    if draw:
        conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM draw_files WHERE draw_date=?", (draw,))
        cursor.execute("DELETE FROM inventory WHERE draw_date=?", (draw,))
        conn.commit()
        conn.close()
        if CURRENT_DRAW_DATE == draw:
            remaining_files = get_visible_draw_files()
            CURRENT_DRAW_DATE = remaining_files[0][0] if remaining_files else ""
    return redirect(url_for('index'))

@app.route("/update_limits", methods=["POST"])
def update_limits():
    conn = sqlite3.connect("ring_inventory.db", timeout=30.0)
    cursor = conn.cursor()
    for k, v in request.form.items():
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)