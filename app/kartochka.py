"""PDF «Карточка перевозчика для грузоотправителя».

Поля и подписи повторяют образец: четыре блока и футер про согласие.
Сканы в файл не вшиваются. Шрифт — Liberation Sans из app/fonts (кириллица есть и без системных шрифтов).
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from app.logic import ApiError

TITLE = "Карточка перевозчика для грузоотправителя"
SUBTITLE = "Данные экспедитора, перевозчика, транспорта и водителя для заявки и пропуска."
FOOTER = "Данные водителя передаются с его согласия только для оформления пропуска и заявки."

KIND_SECTION = {
    "self_employed": "2. ПЕРЕВОЗЧИК (САМОЗАНЯТЫЙ)",
    "ip": "2. ПЕРЕВОЗЧИК (ИП)",
    "ooo": "2. ПЕРЕВОЗЧИК (ООО)",
}
KIND_MARK = {
    "self_employed": "самозанятый",
    "ip": "ИП",
    "ooo": "ООО",
}

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONT_CANDIDATES = (
    (_FONT_DIR / "LiberationSans-Regular.ttf", _FONT_DIR / "LiberationSans-Bold.ttf"),
    (
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ),
    (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ),
)


def ru_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        year, month, day = raw[:10].split("-")
        if year.isdigit() and month.isdigit() and day.isdigit():
            return f"{day}.{month}.{year}"
    return raw


def _join_number(series: str, number: str) -> str:
    return " ".join(part for part in ((series or "").strip(), (number or "").strip()) if part)


def carrier_display_name(carrier: dict) -> str:
    name = (carrier.get("name") or "").strip()
    mark = KIND_MARK.get(carrier.get("kind") or "", "")
    if not mark:
        return name
    token = f"({mark})"
    if token.casefold() in name.casefold():
        return name
    return f"{name} {token}".strip()


def kartochka_sections(expeditor: dict, carrier: dict, vehicle: dict, driver: dict, dispatcher: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Строки PDF в том же порядке, что на образце. Пустое поле остаётся в блоке."""
    account = (expeditor.get("account") or "").strip()
    bik = (expeditor.get("bik") or "").strip()
    corr = (expeditor.get("corr_account") or "").strip()
    account_line = ", ".join(
        part
        for part in (
            account,
            f"БИК {bik}" if bik else "",
            f"к/с {corr}" if corr else "",
        )
        if part
    )
    inn = (expeditor.get("inn") or "").strip()
    ogrnip = (expeditor.get("ogrnip") or "").strip()
    inn_line = " / ".join(part for part in (inn, ogrnip) if part)
    block1 = [
        ("Наименование", expeditor.get("name") or ""),
        ("ИНН / ОГРНИП", inn_line),
        ("Адрес", expeditor.get("address") or ""),
        ("Банк", expeditor.get("bank") or ""),
        ("Р/с, БИК, к/с", account_line),
        ("Телефон", expeditor.get("phone") or ""),
        ("E-mail", expeditor.get("email") or ""),
        ("Диспетчер по рейсу", dispatcher),
        ("Код в ATI.SU", expeditor.get("ati_code") or ""),
        ("ЭДО ID участника:", expeditor.get("edo_id") or ""),
    ]
    block2 = [
        ("Наименование", carrier_display_name(carrier)),
        ("ИНН", carrier.get("inn") or ""),
    ]
    if carrier.get("kind") != "self_employed":
        block2.append(("ОГРН/ОГРНИП", carrier.get("ogrn") or ""))
    block2.append(("Телефон", carrier.get("phone") or ""))
    if (carrier.get("email") or "").strip():
        block2.append(("E-mail", carrier["email"]))
    trailer = (vehicle.get("trailer_plate") or "").strip() or "нет"
    plate = (vehicle.get("plate") or "").strip()
    body = (vehicle.get("body") or "").strip()
    volume = (vehicle.get("volume") or "").strip()
    if body and volume:
        body_line = f"{body}; объём {volume}"
    else:
        body_line = body or (f"объём {volume}" if volume else "")
    block3 = [
        ("Марка, модель", vehicle.get("make_model") or ""),
        ("Кузов, объём", body_line),
        ("Способ погрузки", vehicle.get("loading") or ""),
        ("Госномер ТС / прицепа", f"{plate} / {trailer}" if plate else ""),
        ("VIN", vehicle.get("vin") or ""),
        ("СТС", _join_number(vehicle.get("sts_series") or "", vehicle.get("sts_number") or "")),
    ]
    birth = ru_date(driver.get("birth_date") or "")
    fio = (driver.get("name") or "").strip()
    if birth:
        fio = f"{fio}, {birth} г.р.".strip(", ")
    passport_no = _join_number(driver.get("passport_series") or "", driver.get("passport_number") or "")
    issued_by = (driver.get("passport_issued_by") or "").strip()
    issued_at = ru_date(driver.get("passport_issued_at") or "")
    issued = " ".join(part for part in (issued_by, issued_at) if part)
    passport_bits = [passport_no]
    if issued:
        passport_bits.append(f"выдан {issued}")
    dept = (driver.get("passport_dept_code") or "").strip()
    if dept:
        passport_bits.append(f"код подразделения {dept}")
    license_no = _join_number(driver.get("license_series") or "", driver.get("license_number") or "")
    lic_date = ru_date(driver.get("license_issued_at") or "")
    lic_by = (driver.get("license_issued_by") or "").strip()
    lic_until = ru_date(driver.get("license_valid_until") or "")
    cats = (driver.get("license_categories") or "").strip()
    lic_mid = ""
    if lic_date or lic_by:
        lic_mid = "выдано"
        if lic_date:
            lic_mid += f" {lic_date}"
        if lic_by:
            lic_mid += f" ({lic_by})"
    lic_bits = [license_no]
    if lic_mid:
        lic_bits.append(lic_mid.strip())
    tail = ""
    if lic_until:
        tail = f"действует до {lic_until}"
    if cats:
        tail = f"{tail}; категории {cats}".strip("; ")
    if tail:
        lic_bits.append(tail)
    block4 = [
        ("ФИО", fio),
        ("Телефон", driver.get("phone") or ""),
        ("Паспорт", ", ".join(bit for bit in passport_bits if bit)),
        ("Водительское удостоверение", ", ".join(bit for bit in lic_bits if bit)),
    ]
    section = KIND_SECTION.get(carrier.get("kind") or "", "2. ПЕРЕВОЗЧИК")
    return [
        ("1. ЭКСПЕДИТОР", block1),
        (section, block2),
        ("3. ТРАНСПОРТ", block3),
        ("4. ВОДИТЕЛЬ", block4),
    ]


def _fonts() -> tuple[Path, Path]:
    for regular, bold in _FONT_CANDIDATES:
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise ApiError(500, "Нет шрифта с кириллицей для карточки перевозчика.")


class _Card(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4", unit="mm")
        regular, bold = _fonts()
        self.add_font("Lib", "", str(regular))
        self.add_font("Lib", "B", str(bold))
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(16, 16, 16)

    def section(self, title: str) -> None:
        self.ln(2)
        self.set_font("Lib", "B", 12)
        self.set_text_color(15, 42, 74)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(15, 42, 74)
        self.set_line_width(0.35)
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(2.2)
        self.set_text_color(0, 0, 0)

    def pair(self, label: str, value: str) -> None:
        label_w = 58
        value_w = self.epw - label_w - 2
        text = value.strip() or "—"
        y = self.get_y()
        if y > self.h - 28:
            self.add_page()
            y = self.get_y()
        self.set_xy(self.l_margin, y)
        self.set_font("Lib", "B", 10)
        self.multi_cell(label_w, 5, label, align="L", new_x="RIGHT", new_y="TOP")
        label_bottom = self.get_y()
        self.set_xy(self.l_margin + label_w + 2, y)
        self.set_font("Lib", "", 10)
        self.multi_cell(value_w, 5, text, align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_y(max(self.get_y(), label_bottom) + 1.2)


def render_kartochka(expeditor: dict, carrier: dict, vehicle: dict, driver: dict, dispatcher: str) -> bytes:
    pdf = _Card()
    pdf.add_page()
    pdf.set_font("Lib", "B", 16)
    pdf.set_text_color(15, 42, 74)
    pdf.multi_cell(0, 8, TITLE, align="L")
    pdf.ln(1)
    pdf.set_font("Lib", "", 10)
    pdf.set_text_color(70, 80, 90)
    pdf.multi_cell(0, 5, SUBTITLE, align="L")
    pdf.ln(1)
    pdf.set_text_color(0, 0, 0)
    for title, rows in kartochka_sections(expeditor, carrier, vehicle, driver, dispatcher):
        pdf.section(title)
        for label, value in rows:
            pdf.pair(label, value)
    pdf.ln(3)
    pdf.set_font("Lib", "", 9)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 4.5, FOOTER, align="L")
    return bytes(pdf.output())
