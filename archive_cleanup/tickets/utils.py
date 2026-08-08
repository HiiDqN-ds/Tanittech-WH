import io
import os
from django.conf import settings
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
from reportlab.lib import colors


def generate_pdf(ticket):
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(name="Title", fontSize=18, spaceAfter=15)
    normal = styles["Normal"]

    elements = []

    # -----------------------------
    # HEADER (LOGO + ADDRESS)
    # -----------------------------
    logo_path = os.path.join(settings.BASE_DIR, "media", "images", "logo.png")
    header_data = []

    # Logo column
    if os.path.exists(logo_path):
        logo = Image(logo_path, width=110, height=55)
        header_data.append([logo, Paragraph(
            "<b>TaniTech Repair Center</b><br/>"
            "Adresse: Friedrichstraße 1, 40217 Düsseldorf<br/>"
            "Telefon: +49 123 456789<br/>"
            "E-Mail: info@tanitech.de",
            normal
        )])
    else:
        header_data.append(["", Paragraph(
            "<b>TaniTech Repair Center</b><br/>"
            "Adresse: Friedrichstraße 1, 40217 Düsseldorf<br/>"
            "Telefon: +49 123 456789<br/>"
            "E-Mail: info@tanitech.de",
            normal
        )])

    header_table = Table(header_data, colWidths=[130, 360])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
    ]))

    elements.append(header_table)
    elements.append(Spacer(1, 20))

    # -----------------------------
    # TITLE
    # -----------------------------
    elements.append(Paragraph("<b>Reparaturauftrag / Repair Contract</b>", title_style))
    elements.append(Spacer(1, 10))

    # -----------------------------
    # MAIN INFO TABLE
    # -----------------------------
    data = [
        ["Tracking Nummer", ticket.tracking_id],
        ["Kunde", f"{ticket.client.first_name} {ticket.client.last_name}"],
        ["Email", ticket.client.email],
        ["Telefon", ticket.client_phone or "-"],
        ["Gerät", f"{ticket.device_type} {ticket.device_model}"],
        ["Problem", ticket.description or "-"],
        ["Preis", f"{ticket.estimated_price} € (inkl. 19% MwSt.)"],
        ["MwSt (19%)", f"{float(ticket.estimated_price) * 0.19:.2f} €"]
    ]

    table = Table(data, colWidths=[160, 340])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("BOX", (0, 0), (-1, -1), 1, colors.grey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 20))

    # -----------------------------
    # TERMS / AGB
    # -----------------------------
    agreement_text = """
    <b>ALLGEMEINE GESCHÄFTSBEDINGUNGEN</b><br/><br/>
    Bei der Reparatur Ihres Mobiltelefons, Tablets oder Notebooks kann es zum Verlust aller gespeicherten Daten kommen.
    Des Weiteren kann es bei bestimmten Reparaturen zu einem Board- oder Softwareschaden kommen, welcher sich als Blue oder Red Screen zeigen kann.
    Bei Sturzschäden können Folgeschäden entstehen, welche erst im Laufe der Reparatur sichtbar werden.
    Wenn durch den Sturz das Backcover / der Rahmen verzogen sind, kann es dazu kommen, dass das neue Display nicht mehr einhundertprozentig passt,
    wofür TaniTech nicht haftbar gemacht werden kann.<br/><br/>

    Wenn Ihr Smartphone „gejailbreakt“ oder „gerootet“ ist, kann es nach einer Reparatur zu Folgeschäden kommen.
    TaniTech kann für daraus entstandene Schäden keine Haftung übernehmen.<br/><br/>

    Eine Hersteller-Garantie kann durch die Reparatur erlöschen.
    Die von uns verbauten Ersatzteile gelten nach Einbau nicht mehr als neu und sind somit von der Rückgabe ausgeschlossen.<br/><br/>

    Geräte mit Wasserschaden: Wir können für die Funktionstüchtigkeit nicht garantieren.
    Geräte können trotz erfolgreicher Reparatur Wochen oder Monate später erneut ausfallen.<br/><br/>

    Mit Ihrer Auftragserteilung bestätigen Sie die Kenntnisnahme und Anerkennung unserer AGB.
    Mit der Abgabe des Geräts bestätigt der Kunde, dass alle Angaben korrekt sind.
    Tanitech übernimmt keine Haftung für Datenverlust. Reparaturen erfolgen nur nach Freigabe.
    """

    elements.append(Paragraph(agreement_text, normal))
    elements.append(Spacer(1, 30))

    confirmation_text = """
    <br/><br/>
    <b>WICHTIGER HINWEIS:</b><br/>
    Mit der Abgabe oder Einsendung des Geräts erklärt sich der Kunde mit diesen Bedingungen einverstanden.
    Der angegebene Preis ist ein Richtwert und kann sich nach der Diagnose ändern.
    Eine Reparatur erfolgt nur nach Freigabe durch den Kunden.
    """
    elements.append(Paragraph(agreement_text + confirmation_text, normal))
    elements.append(Spacer(1, 30))
  

    doc.build(elements)
    buffer.seek(0)
    return buffer
