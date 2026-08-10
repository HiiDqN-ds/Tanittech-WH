/*
 * Assistant chat widget — reads from and writes to the database through the
 * /assistant/api/* endpoints. Three ways to drive it:
 *   1. Buttons (menus / inline forms) — always available, most predictable.
 *   2. Free-text chat — any phrasing goes to a real Claude-powered backend
 *      (POST /assistant/api/chat), which has tool access to the same
 *      read/write actions as the buttons (add a debt, pay an invoice, sell
 *      an item, ...) and replies conversationally, in German, English or
 *      Arabic — whichever the message was written in.
 *   3. Voice — the mic button records audio and sends it to the server
 *      (POST /assistant/api/transcribe, OpenAI Whisper) to transcribe into
 *      the same free-text pipeline, then (optionally) speaks the answer
 *      back.
 * One single chat, not one-per-language: the chat's own UI chrome (input
 * placeholder, mic language, RTL layout) switches instantly based on a
 * quick client-side language guess, while the AI decides the language of
 * its own replies independently, so the two can never fall out of sync.
 * The current screen persists in localStorage, so navigating to a
 * different page of the app does not reset the chat, and the in-chat
 * "back" button steps back through the actual menu it came from instead
 * of always jumping to the main menu. The conversation itself (what the
 * AI remembers) is a real server-side record, not just browser memory —
 * see /assistant/api/history and /assistant/api/conversations — organized
 * into GPT-style separate conversations the person can switch between via
 * the header's "New chat" / "History" buttons.
 */
(function () {
  const panel = document.getElementById('assistantPanel');
  const launcher = document.getElementById('assistantLauncher');
  const body = document.getElementById('assistantBody');
  const closeBtn = document.getElementById('assistantClose');
  const backBtn = document.getElementById('assistantBack');
  const titleEl = document.getElementById('assistantTitle');
  const inputForm = document.getElementById('assistantInputForm');
  const textInput = document.getElementById('assistantTextInput');
  const micBtn = document.getElementById('assistantMicBtn');
  const headerLangSwitch = document.getElementById('headerLangSwitch');
  const newChatBtn = document.getElementById('assistantNewChatBtn');
  const historyBtn = document.getElementById('assistantHistoryBtn');

  if (!panel || !launcher || !body) return;

  const STORAGE_KEY = window.ASSISTANT_STORAGE_KEY || 'ria_assistant_state_v1';
  const MAX_STORED_MESSAGES = 60;

  // ---------------------------------------------------------------------
  // Role-based permissions
  // ---------------------------------------------------------------------
  // Mirrors PERMISSION_CATEGORY_TOOLS + SELLER_BASE_TOOLS in app.py. The
  // backend is the actual enforcement point (_execute_assistant_tool
  // rejects any tool call outside seller_allowed_tools() for a non-admin
  // session) — this copy exists purely so the *menu* a seller sees
  // doesn't offer buttons that would just come back as a permission
  // error. If you add/remove a tool from a category in app.py, update
  // the matching set below, or sellers will either see options that
  // fail, or miss options they should have.
  const ROLE = window.ASSISTANT_ROLE === 'admin' ? 'admin' : 'seller';
  // Per-seller "KI-Assistent — zusätzliche Berechtigungen" grants (set in
  // base.html/chat_app.html from the `seller_categories` context var —
  // see seller_granted_categories() in app.py). Admins are never
  // restricted by this; array of category keys for a seller, e.g. ["kasse"].
  const GRANTED_CATEGORIES = new Set(
    ROLE === 'admin' ? [] : (window.ASSISTANT_PERMS || [])
  );
  const CAN_KASSE = ROLE === 'admin' || GRANTED_CATEGORIES.has('kasse');
  const CATEGORY_TOOLS = {
    debts: ['add_debt', 'pay_debt', 'record_debt_payment', 'delete_debt',
            'delete_all_debts', 'update_debt', 'edit_debt_payment', 'delete_debt_payment'],
    factures: ['add_facture', 'pay_facture', 'edit_facture', 'delete_facture'],
    kasse: ['add_kasse_transaction', 'edit_kasse_transaction', 'delete_kasse_transaction'],
    items: ['add_item', 'update_item', 'delete_item'],
    orders: ['add_order', 'edit_order', 'delete_order'],
    sales: ['edit_sale_item', 'delete_sale'],
    clients: ['rename_client', 'delete_client'],
  };
  const SELLER_ALLOWED_TOOLS = new Set([
    'get_summary', 'list_items', 'list_low_stock', 'list_recent_sales',
    'list_recent_orders', 'quick_sell', 'print_barcode', 'remember_note',
  ]);
  for (const [category, tools] of Object.entries(CATEGORY_TOOLS)) {
    if (GRANTED_CATEGORIES.has(category)) tools.forEach(t => SELLER_ALLOWED_TOOLS.add(t));
  }

  function canUseTool(toolName) {
    return ROLE === 'admin' || SELLER_ALLOWED_TOOLS.has(toolName);
  }


  // ---------------------------------------------------------------------
  // i18n
  // ---------------------------------------------------------------------
  const SPEECH_LANG = { de: 'de-DE', en: 'en-US', ar: 'ar-SA' };
  const LOCALE = { de: 'de-DE', en: 'en-US', ar: 'ar-EG' };

  const I18N = {
    de: {
      title: 'Assistent',
      launcher_label: 'Assistent öffnen',
      close_label: 'Schließen',
      input_placeholder: 'Nachricht schreiben oder Mikrofon nutzen…',
      listening_placeholder: '🎙️ Ich höre zu…',
      greeting: '👋 Hallo! Ich bin Ihr Assistent. Was möchten Sie tun?',
      ask_next: 'Was möchten Sie als Nächstes tun?',
      not_understood: '🤔 Das habe ich nicht verstanden. Wählen Sie eine Option:',
      generic_error: '❌ Etwas ist schiefgelaufen.',
      connection_error: 'Verbindung zum Server fehlgeschlagen.',
      history_title: '📜 Verlauf',
      history_none: 'Noch keine früheren Unterhaltungen.',
      history_load: 'Öffnen',
      history_delete: 'Löschen',
      history_confirm_delete: 'Diese Unterhaltung wirklich unwiderruflich löschen?',
      history_untitled: 'Neue Unterhaltung',
      new_chat_started: '✨ Neue Unterhaltung gestartet.',
      widgets: {
        sales_today: 'Umsatz heute', profit_today: 'Gewinn heute', sales_month: 'Umsatz Monat',
        kasse_balance: 'Kassenstand', debts_open: 'Offene Schulden', low_stock: 'Niedriger Bestand',
        date: 'Datum', user: 'Verkäufer', items: 'Artikel', total: 'Summe',
        client: 'Kunde', amount: 'Betrag', description: 'Beschreibung',
        product: 'Produkt', qty: 'Menge',
      },
      confirm_prompt: 'Bitte bestätigen:',
      confirm_yes: '✅ Ja, ausführen',
      confirm_cancel: '✖ Abbrechen',
      confirm_cancelled: 'Abgebrochen — es wurde nichts geändert.',
      mic_unsupported: '🎙️ Spracheingabe wird von diesem Browser nicht unterstützt. Bitte Chrome verwenden oder weiter tippen.',
      mic_permission_denied: '🎙️ Zugriff auf das Mikrofon wurde verweigert. Bitte in den Browser-/Website-Einstellungen erlauben.',
      mic_error: '🎙️ Spracheingabe hat nicht funktioniert. Bitte erneut versuchen oder tippen.',
      mic_too_short: '🎙️ Das war zu kurz oder ich habe nichts gehört. Bitte etwas länger sprechen.',
      back: '⬅ Zurück',
      menu: {
        report: '📊 Bericht',
        debts: '💸 Schulden',
        factures: '🧾 Rechnungen',
        stock: '📦 Lagerbestand',
        kasse: '💰 Kasse',
        items: '📦 Artikel',
        sellers: '👤 Verkäufer',
        salary: '💵 Gehalt',
        orders: '🛒 Einkauf',
        sales: '🧾 Meine Verkäufe',
        sell: '💳 Verkaufen',
      },
      report: {
        which: 'Welchen Bericht möchten Sie sehen?',
        overview: '📈 Gesamtübersicht',
        title: '📊 Gesamtübersicht',
        sales_today: 'Verkäufe heute',
        profit_today: 'Gewinn heute',
        sales_month: 'Verkäufe diesen Monat',
        kasse_balance: 'Kassenstand',
        debts_open: 'Offene Schulden',
        factures_unpaid: 'Offene Rechnungen',
        low_stock: 'Niedriger Lagerbestand',
        items: 'Artikel',
      },
      debts: {
        menu_title: 'Schulden — was möchten Sie tun?',
        view: '📋 Offene Schulden anzeigen',
        add: '➕ Neue Schuld hinzufügen',
        none: '🎉 Keine offenen Schulden vorhanden.',
        count: (n) => `Es gibt <strong>${n}</strong> offene Schuld(en):`,
        mark_paid: '✅ Als bezahlt markieren',
        marked: '✔ Erledigt',
        added: (name, amount) => `✅ Schuld über ${amount} für ${name} wurde hinzugefügt.`,
        paid_voice: (name) => `✅ Schuld von ${name} wurde als bezahlt markiert.`,
        not_found: (name) => `❌ Ich konnte keine offene Schuld für "${name}" finden.`,
        need_info: '⚠️ Bitte Name und Betrag angeben, z. B. „Schuld hinzufügen Ahmed 50“.',
        add_more: '➕ Weitere hinzufügen',
        view_clients: '👥 Kunden anzeigen',
        delete_all: '🗑 Alle Schulden löschen',
        confirm_delete_all: '⚠️ Wirklich <strong>ALLE</strong> Schulden aller Kunden unwiderruflich löschen? Das kann nicht rückgängig gemacht werden.',
        deleted_all: (n) => `🗑 ${n} Schuld(en) wurden gelöscht.`,
        edit_btn: '✏️ Bearbeiten',
        edited: (name) => `✅ Schuld von ${name} wurde aktualisiert.`,
        reference_label: 'Ref.',
        clients: {
          none: '🎉 Es sind noch keine Kunden erfasst.',
          list_title: (n) => `<strong>${n}</strong> Kunde(n):`,
          history_title: (name) => `Schulden von <strong>${escapeHtml(name)}</strong>:`,
          no_debts: 'Für diesen Kunden liegen keine Schulden vor.',
          paid_label: 'Bezahlt',
          open_label: 'Offen',
          back_to_clients: '⬅ Zur Kundenliste',
        },
        form: {
          client: 'Kunde', client_ph: 'Name des Kunden',
          amount: 'Betrag (€)',
          phone: 'Telefon', phone_ph: 'Optional',
          description: 'Beschreibung', description_ph: 'z. B. Warenkredit',
          reference: 'Referenznummer',
          save: 'Speichern', saving: 'Speichere...',
          payment_method_label: 'Zahlungsmethode', payment_cash: 'Bar', payment_card: 'Karte',
          card_not_configured: 'Kartenzahlungen sind nicht eingerichtet (STRIPE_SECRET_KEY fehlt).',
          card_reader_discovering: 'Kartenlesegerät wird gesucht...',
          card_reader_connecting: 'Verbindung zum Kartenlesegerät wird hergestellt...',
          card_creating_intent: 'Zahlung wird vorbereitet...',
          card_present_card: 'Bitte Karte am Lesegerät vorhalten oder stecken...',
          card_processing: 'Zahlung wird verarbeitet...',
          card_error_prefix: 'Fehler:',
        },
      },
      factures: {
        menu_title: 'Rechnungen — was möchten Sie tun?',
        view: '📋 Offene Rechnungen anzeigen',
        add: '➕ Neue Rechnung hinzufügen',
        none: '🎉 Keine offenen Rechnungen vorhanden.',
        count: (n) => `Es gibt <strong>${n}</strong> offene Rechnung(en):`,
        mark_paid: '✅ Als bezahlt markieren',
        marked: '✔ Erledigt',
        due: 'Fällig',
        add_more: '➕ Weitere hinzufügen',
        edit_btn: '✏️ Bearbeiten',
        delete_btn: '🗑 Löschen',
        edited: (name) => `✅ Rechnung von ${name} wurde aktualisiert.`,
        confirm_delete: (name) => `⚠️ Rechnung von <strong>${name}</strong> wirklich unwiderruflich löschen?`,
        deleted: '🗑 Rechnung wurde gelöscht.',
        search_ph: '🔎 Nach Aussteller suchen…',
        load_more: (n) => `⬇️ Weitere laden (noch ${n})`,
        showing: (shown, total) => `${shown} von ${total}`,
        types: {
          ebay: 'eBay', electricity: 'Strom / Électricité', water: 'Wasser / Eau',
          internet: 'Internet', phone: 'Telefon', supplier: 'Lieferant',
          rent: 'Miete', other: 'Sonstiges',
        },
        form: {
          type: 'Typ', issuer: 'Aussteller', issuer_ph: 'z. B. eBay, EDF, Vodafone',
          amount: 'Betrag (€)', due_date: 'Fällig am',
          save: 'Speichern', saving: 'Speichere...',
          edit_title: 'Rechnung bearbeiten', saving_edit: 'Aktualisiere...',
        },
      },
      stock: {
        menu_title: 'Lagerbestand — was möchten Sie tun?',
        view: '⚠️ Niedrigen Bestand anzeigen',
        none: '✅ Kein Artikel hat einen niedrigen Bestand.',
        warning: '⚠️ Folgende Artikel haben niedrigen Bestand:',
      },
      kasse: {
        menu_title: 'Kasse — was möchten Sie tun?',
        view: '💰 Heutigen Kassenstand anzeigen',
        history: '📜 Verlauf anzeigen',
        history_title: (n) => `<strong>${n}</strong> letzte Kassenbuchungen:`,
        history_none: 'Keine Kassenbuchungen gefunden.',
        add: '➕ Ein-/Auszahlung buchen',
        title: '💰 Kasse heute',
        balance: 'Kassenstand',
        sales_today: 'Verkäufe heute',
        purchases_today: 'Einkäufe heute',
        deposits_today: 'Einzahlungen heute',
        withdrawals_today: 'Auszahlungen heute',
        types: { einzahlung: 'Einzahlung', auszahlung: 'Auszahlung' },
        form: { type: 'Typ', amount: 'Betrag (€)', description: 'Beschreibung', description_ph: 'z. B. Wechselgeld', save: 'Speichern', saving: 'Speichere...' },
        booked: (typ, amount) => `✅ ${typ} über ${amount} wurde gebucht.`,
      },
      items: {
        menu_title: 'Artikel — was möchten Sie tun?',
        view: '📋 Artikel anzeigen',
        add: '➕ Neuen Artikel hinzufügen',
        low_stock: '⚠️ Niedrigen Bestand anzeigen',
        none: 'Keine Artikel gefunden.',
        list_title: (n) => `<strong>${n}</strong> Artikel gefunden:`,
        search_ph: '🔍 Suche nach Barcode oder Name…',
        added: (name, code) => `✅ Artikel "${name}" wurde hinzugefügt (Barcode ${code}).`,
        add_more: '➕ Weiteren hinzufügen',
        edit_btn: (name) => `✏️ ${name} bearbeiten`,
        delete_btn: '🗑 Löschen',
        confirm_delete: (name) => `⚠️ Artikel <strong>${name}</strong> wirklich unwiderruflich löschen?`,
        deleted: '🗑 Artikel wurde gelöscht.',
        form: {
          name: 'Produktname', name_ph: 'z. B. Cola 0,5 L',
          quantity: 'Menge', purchase_price: 'Einkaufspreis (€)', selling_price: 'Verkaufspreis (€)',
          barcode: 'Barcode', barcode_ph: 'Leer lassen = automatisch',
          save: 'Speichern', saving: 'Speichere...',
        },
      },
      sellers: {
        menu_title: 'Verkäufer — was möchten Sie tun?',
        view: '📋 Verkäufer anzeigen',
        add: '➕ Neuen Verkäufer hinzufügen',
        none: 'Keine Verkäufer gefunden.',
        list_title: (n) => `<strong>${n}</strong> Verkäufer:`,
        added: (name) => `✅ Verkäufer "${name}" wurde hinzugefügt.`,
        add_more: '➕ Weiteren hinzufügen',
        edit_btn: (name) => `✏️ ${name} bearbeiten`,
        delete_btn: '🗑 Löschen',
        confirm_delete: (name) => `⚠️ Verkäufer <strong>${name}</strong> wirklich unwiderruflich löschen?`,
        deleted: '🗑 Verkäufer wurde gelöscht.',
        active: 'aktiv', inactive: 'inaktiv',
        form: {
          username: 'Benutzername', password: 'Passwort', salary: 'Gehalt (€)',
          save: 'Speichern', saving: 'Speichere...',
        },
      },
      salary: {
        menu_title: 'Gehalt — was möchten Sie tun?',
        view: '📋 Letzte Zahlungen anzeigen',
        add: '➕ Gehalt auszahlen',
        none: 'Keine Zahlungen gefunden.',
        list_title: (n) => `<strong>${n}</strong> letzte Zahlung(en):`,
        paid: (name, amount) => `✅ ${amount} wurden an ${name} bezahlt.`,
        add_more: '➕ Weitere Zahlung',
        select_ph: 'Bitte auswählen…',
        no_employees: 'Keine Verkäufer gefunden. Bitte zuerst einen Verkäufer anlegen.',
        form: {
          employee: 'Mitarbeiter', amount: 'Betrag (€)', source: 'Quelle',
          save: 'Auszahlen', saving: 'Speichere...',
        },
      },
      scan: {
        button: '📷 Rechnung fotografieren / PDF',
        uploading: '📷 Wird gelesen (OCR/PDF)…',
        failed: '❌ Konnte die Datei nicht lesen. Bitte Felder manuell ausfüllen oder ein schärferes Foto versuchen.',
        found: '📸 Ich habe folgende Angaben erkannt — bitte prüfen und speichern:',
        check_amount: '⚠️ Den Betrag bitte kurz prüfen — ich habe keine eindeutige "Gesamtbetrag"-Angabe gefunden und daher die größte Zahl auf der Seite geraten.',
      },
      orders: {
        menu_title: 'Einkauf — was möchten Sie tun?',
        view: '📋 Letzte Bestellungen anzeigen',
        add: '➕ Neue Bestellung aufgeben',
        none: 'Keine Bestellungen gefunden.',
        list_title: (n) => `<strong>${n}</strong> letzte Bestellung(en):`,
        added: (name, qty, total) => `✅ Bestellung "${name}" (${qty} Stück, ${total}) wurde gespeichert.`,
        add_more: '➕ Weitere Bestellung',
        edit_btn: (n) => `✏️ Bestellung #${n} bearbeiten`,
        delete_btn: '🗑 Löschen',
        confirm_delete: (n) => `⚠️ Bestellung <strong>#${n}</strong> wirklich unwiderruflich löschen?`,
        deleted: '🗑 Bestellung wurde gelöscht.',
        search_ph: '🔍 Suche nach Barcode oder Produktname…',
        form: {
          product_name: 'Produktname', product_name_ph: 'z. B. Cola 0,5 L',
          price: 'Einkaufspreis (€)', selling_price: 'Verkaufspreis (€)',
          quantity: 'Menge', ref_number: 'Referenznummer', ref_number_ph: 'optional',
          save: 'Bestellung speichern', saving: 'Speichere...',
          payment_method_label: 'Zahlungsmethode', payment_cash: 'Bar', payment_card: 'Karte',
          card_not_configured: 'Kartenzahlungen sind nicht eingerichtet (STRIPE_SECRET_KEY fehlt).',
          card_reader_discovering: 'Kartenlesegerät wird gesucht...',
          card_reader_connecting: 'Verbindung zum Kartenlesegerät wird hergestellt...',
          card_creating_intent: 'Zahlung wird vorbereitet...',
          card_present_card: 'Bitte Karte am Lesegerät vorhalten oder stecken...',
          card_processing: 'Zahlung wird verarbeitet...',
          card_error_prefix: 'Fehler:',
        },
      },
      sales: {
        menu_title: 'Meine Verkäufe — was möchten Sie tun?',
        view: '📋 Letzte Verkäufe anzeigen',
        none: 'Keine Verkäufe gefunden.',
        list_title: (n) => `<strong>${n}</strong> letzte Verkäufe:`,
        row: (n, total, items) => `#${n} — ${total} (${items} Artikel)`,
        delete_btn: '🗑 Löschen',
        delete_confirm: (n) => `Verkauf #${n} wirklich löschen? Der Lagerbestand wird nicht automatisch zurückgebucht.`,
        deleted: '✅ Verkauf wurde gelöscht.',
        edit_btn: '✏️ Bearbeiten',
        search_ph: '🔍 Suche nach Barcode oder Artikelname…',
      },
      sell: {
        menu_title: 'Verkaufen — was möchten Sie tun?',
        quick: '⚡ Schnellverkauf (ein Artikel)',
        none: 'Kein passender Artikel gefunden.',
        sold: (qty, name, total) => `✅ ${qty} × ${name} verkauft für insgesamt ${total}.`,
        low_stock: (n) => `⚠️ Nur noch ${n} Stück auf Lager!`,
        not_enough_stock: (n) => `Nur ${n} Stück verfügbar.`,
        print_barcode: '🖨️ Barcode drucken',
        barcode_title: (name) => `Barcode für ${name}:`,
        print_btn: '🖨️ Drucken',
        form: {
          identifier: 'Produktname oder Barcode', identifier_ph: 'z. B. Cola 0,5 L',
          quantity: 'Menge',
          save: 'Verkaufen', saving: 'Verkaufe...',
          payment_method_label: 'Zahlungsmethode', payment_cash: 'Bar', payment_card: 'Karte',
          card_not_configured: 'Kartenzahlungen sind nicht eingerichtet (STRIPE_SECRET_KEY fehlt).',
          card_reader_discovering: 'Kartenlesegerät wird gesucht...',
          card_reader_connecting: 'Verbindung zum Kartenlesegerät wird hergestellt...',
          card_creating_intent: 'Zahlung wird vorbereitet...',
          card_present_card: 'Bitte Karte am Lesegerät vorhalten oder stecken...',
          card_processing: 'Zahlung wird verarbeitet...',
          card_error_prefix: 'Fehler:',
        },
      },
    },
    en: {
      title: 'Assistant',
      launcher_label: 'Open assistant',
      close_label: 'Close',
      input_placeholder: 'Type a message or use the microphone…',
      listening_placeholder: '🎙️ Listening…',
      greeting: '👋 Hi! I\'m your assistant. What would you like to do?',
      ask_next: 'What would you like to do next?',
      not_understood: '🤔 I didn\'t quite catch that. Please choose an option:',
      generic_error: '❌ Something went wrong.',
      connection_error: 'Could not connect to the server.',
      history_title: '📜 History',
      history_none: 'No past conversations yet.',
      history_load: 'Open',
      history_delete: 'Delete',
      history_confirm_delete: 'Really permanently delete this conversation?',
      history_untitled: 'New conversation',
      new_chat_started: '✨ New conversation started.',
      widgets: {
        sales_today: 'Sales today', profit_today: 'Profit today', sales_month: 'Sales this month',
        kasse_balance: 'Cash balance', debts_open: 'Open debts', low_stock: 'Low stock',
        date: 'Date', user: 'Seller', items: 'Items', total: 'Total',
        client: 'Client', amount: 'Amount', description: 'Description',
        product: 'Product', qty: 'Qty',
      },
      confirm_prompt: 'Please confirm:',
      confirm_yes: '✅ Yes, do it',
      confirm_cancel: '✖ Cancel',
      confirm_cancelled: 'Cancelled — nothing was changed.',
      mic_unsupported: "🎙️ Voice input isn't supported in this browser. Try Chrome, or keep typing.",
      mic_permission_denied: '🎙️ Microphone access was denied. Please allow it in your browser/site settings.',
      mic_error: '🎙️ Voice input didn\'t work. Please try again or type instead.',
      mic_too_short: "🎙️ That was too short or I didn't catch anything. Please try speaking a bit longer.",
      back: '⬅ Back',
      menu: {
        report: '📊 Report',
        debts: '💸 Debts',
        factures: '🧾 Invoices',
        stock: '📦 Stock',
        kasse: '💰 Cash register',
        items: '📦 Items',
        sellers: '👤 Sellers',
        salary: '💵 Salary',
        orders: '🛒 Purchasing',
        sales: '🧾 My Sales',
        sell: '💳 Sell',
      },
      report: {
        which: 'Which report would you like to see?',
        overview: '📈 Overview',
        title: '📊 Overview',
        sales_today: 'Sales today',
        profit_today: 'Profit today',
        sales_month: 'Sales this month',
        kasse_balance: 'Cash balance',
        debts_open: 'Open debts',
        factures_unpaid: 'Unpaid invoices',
        low_stock: 'Low stock',
        items: 'items',
      },
      debts: {
        menu_title: 'Debts — what would you like to do?',
        view: '📋 Show open debts',
        add: '➕ Add a new debt',
        none: '🎉 No open debts.',
        count: (n) => `There ${n === 1 ? 'is' : 'are'} <strong>${n}</strong> open debt(s):`,
        mark_paid: '✅ Mark as paid',
        marked: '✔ Done',
        added: (name, amount) => `✅ Debt of ${amount} for ${name} was added.`,
        paid_voice: (name) => `✅ ${name}'s debt was marked as paid.`,
        not_found: (name) => `❌ I couldn't find an open debt for "${name}".`,
        need_info: '⚠️ Please give a name and an amount, e.g. "add debt John 50".',
        add_more: '➕ Add another',
        view_clients: '👥 View clients',
        delete_all: '🗑 Delete all debts',
        confirm_delete_all: '⚠️ Really delete <strong>ALL</strong> debts for every client, permanently? This cannot be undone.',
        deleted_all: (n) => `🗑 ${n} debt(s) were deleted.`,
        edit_btn: '✏️ Edit',
        edited: (name) => `✅ ${name}'s debt was updated.`,
        reference_label: 'Ref.',
        clients: {
          none: '🎉 No clients recorded yet.',
          list_title: (n) => `<strong>${n}</strong> client(s):`,
          history_title: (name) => `Debts of <strong>${escapeHtml(name)}</strong>:`,
          no_debts: 'This client has no debts on record.',
          paid_label: 'Paid',
          open_label: 'Open',
          back_to_clients: '⬅ Back to clients',
        },
        form: {
          client: 'Client', client_ph: 'Client name',
          amount: 'Amount (€)',
          phone: 'Phone', phone_ph: 'Optional',
          description: 'Description', description_ph: 'e.g. store credit',
          reference: 'Reference number',
          save: 'Save', saving: 'Saving...',
          payment_method_label: 'Payment method', payment_cash: 'Cash', payment_card: 'Card',
          card_not_configured: 'Card payments are not set up (STRIPE_SECRET_KEY missing).',
          card_reader_discovering: 'Looking for the card reader...',
          card_reader_connecting: 'Connecting to the card reader...',
          card_creating_intent: 'Preparing the payment...',
          card_present_card: 'Please present or insert the card on the reader...',
          card_processing: 'Processing payment...',
          card_error_prefix: 'Error:',
        },
      },
      factures: {
        menu_title: 'Invoices — what would you like to do?',
        view: '📋 Show unpaid invoices',
        add: '➕ Add a new invoice',
        none: '🎉 No unpaid invoices.',
        count: (n) => `There ${n === 1 ? 'is' : 'are'} <strong>${n}</strong> unpaid invoice(s):`,
        mark_paid: '✅ Mark as paid',
        marked: '✔ Done',
        due: 'Due',
        add_more: '➕ Add another',
        edit_btn: '✏️ Edit',
        delete_btn: '🗑 Delete',
        edited: (name) => `✅ ${name}'s invoice was updated.`,
        confirm_delete: (name) => `⚠️ Permanently delete the invoice from <strong>${name}</strong>?`,
        deleted: '🗑 Invoice deleted.',
        search_ph: '🔎 Search by issuer…',
        load_more: (n) => `⬇️ Load more (${n} left)`,
        showing: (shown, total) => `${shown} of ${total}`,
        types: {
          ebay: 'eBay', electricity: 'Electricity', water: 'Water',
          internet: 'Internet', phone: 'Phone', supplier: 'Supplier',
          rent: 'Rent', other: 'Other',
        },
        form: {
          type: 'Type', issuer: 'Issuer', issuer_ph: 'e.g. eBay, EDF, Vodafone',
          amount: 'Amount (€)', due_date: 'Due date',
          save: 'Save', saving: 'Saving...',
          edit_title: 'Edit invoice', saving_edit: 'Updating...',
        },
      },
      stock: {
        menu_title: 'Stock — what would you like to do?',
        view: '⚠️ Show low stock',
        none: '✅ No items are low on stock.',
        warning: '⚠️ These items are low on stock:',
      },
      kasse: {
        menu_title: 'Cash register — what would you like to do?',
        view: '💰 Show today\'s cash balance',
        history: '📜 Show history',
        history_title: (n) => `<strong>${n}</strong> recent cash register entries:`,
        history_none: 'No cash register entries found.',
        add: '➕ Record a deposit/withdrawal',
        title: '💰 Cash register today',
        balance: 'Cash balance',
        sales_today: 'Sales today',
        purchases_today: 'Purchases today',
        deposits_today: 'Deposits today',
        withdrawals_today: 'Withdrawals today',
        types: { einzahlung: 'Deposit', auszahlung: 'Withdrawal' },
        form: { type: 'Type', amount: 'Amount (€)', description: 'Description', description_ph: 'e.g. change fund', save: 'Save', saving: 'Saving...' },
        booked: (typ, amount) => `✅ ${typ} of ${amount} was recorded.`,
      },
      items: {
        menu_title: 'Items — what would you like to do?',
        view: '📋 Show items',
        add: '➕ Add a new item',
        low_stock: '⚠️ Show low stock',
        none: 'No items found.',
        list_title: (n) => `<strong>${n}</strong> item(s) found:`,
        search_ph: '🔍 Search by barcode or name…',
        added: (name, code) => `✅ Item "${name}" was added (barcode ${code}).`,
        add_more: '➕ Add another',
        edit_btn: (name) => `✏️ Edit ${name}`,
        delete_btn: '🗑 Delete',
        confirm_delete: (name) => `⚠️ Really permanently delete item <strong>${name}</strong>?`,
        deleted: '🗑 Item was deleted.',
        form: {
          name: 'Product name', name_ph: 'e.g. Cola 0.5 L',
          quantity: 'Quantity', purchase_price: 'Purchase price (€)', selling_price: 'Selling price (€)',
          barcode: 'Barcode', barcode_ph: 'Leave empty = automatic',
          save: 'Save', saving: 'Saving...',
        },
      },
      sellers: {
        menu_title: 'Sellers — what would you like to do?',
        view: '📋 Show sellers',
        add: '➕ Add a new seller',
        none: 'No sellers found.',
        list_title: (n) => `<strong>${n}</strong> seller(s):`,
        added: (name) => `✅ Seller "${name}" was added.`,
        add_more: '➕ Add another',
        edit_btn: (name) => `✏️ Edit ${name}`,
        delete_btn: '🗑 Delete',
        confirm_delete: (name) => `⚠️ Really permanently delete seller <strong>${name}</strong>?`,
        deleted: '🗑 Seller was deleted.',
        active: 'active', inactive: 'inactive',
        form: {
          username: 'Username', password: 'Password', salary: 'Salary (€)',
          save: 'Save', saving: 'Saving...',
        },
      },
      salary: {
        menu_title: 'Salary — what would you like to do?',
        view: '📋 Show recent payments',
        add: '➕ Pay salary',
        none: 'No payments found.',
        list_title: (n) => `<strong>${n}</strong> recent payment(s):`,
        paid: (name, amount) => `✅ ${amount} was paid to ${name}.`,
        add_more: '➕ Another payment',
        select_ph: 'Please select…',
        no_employees: 'No sellers found. Add a seller first.',
        form: {
          employee: 'Employee', amount: 'Amount (€)', source: 'Source',
          save: 'Pay', saving: 'Saving...',
        },
      },
      scan: {
        button: '📷 Photo or PDF of the invoice',
        uploading: '📷 Reading it (OCR/PDF)…',
        failed: '❌ Could not read the file. Please fill the fields manually or try a sharper photo.',
        found: '📸 Here\'s what I recognised — please check and save:',
        check_amount: '⚠️ Please double-check the amount — I couldn\'t find a clear "Total"/"Amount Due" label, so I guessed the largest number on the page.',
      },
      orders: {
        menu_title: 'Purchasing — what would you like to do?',
        view: '📋 Show recent purchase orders',
        add: '➕ Place a new purchase order',
        none: 'No purchase orders found.',
        list_title: (n) => `<strong>${n}</strong> recent order(s):`,
        added: (name, qty, total) => `✅ Order "${name}" (${qty} pcs, ${total}) was saved.`,
        add_more: '➕ Add another order',
        edit_btn: (n) => `✏️ Edit order #${n}`,
        delete_btn: '🗑 Delete',
        confirm_delete: (n) => `⚠️ Really permanently delete order <strong>#${n}</strong>?`,
        deleted: '🗑 Order was deleted.',
        search_ph: '🔍 Search by barcode or product name…',
        form: {
          product_name: 'Product name', product_name_ph: 'e.g. Coke 0.5 L',
          price: 'Purchase price (€)', selling_price: 'Selling price (€)',
          quantity: 'Quantity', ref_number: 'Reference number', ref_number_ph: 'optional',
          save: 'Save order', saving: 'Saving...',
          payment_method_label: 'Payment method', payment_cash: 'Cash', payment_card: 'Card',
          card_not_configured: 'Card payments are not set up yet (STRIPE_SECRET_KEY missing).',
          card_reader_discovering: 'Looking for the card reader...',
          card_reader_connecting: 'Connecting to the card reader...',
          card_creating_intent: 'Preparing the payment...',
          card_present_card: 'Please present or insert the card on the reader...',
          card_processing: 'Processing the payment...',
          card_error_prefix: 'Error:',
        },
      },
      sales: {
        menu_title: 'My Sales — what would you like to do?',
        view: '📋 Show recent sales',
        none: 'No sales found.',
        list_title: (n) => `<strong>${n}</strong> recent sale(s):`,
        row: (n, total, items) => `#${n} — ${total} (${items} item(s))`,
        delete_btn: '🗑 Delete',
        delete_confirm: (n) => `Delete sale #${n}? Stock quantities are not automatically restored.`,
        deleted: '✅ Sale deleted.',
        edit_btn: '✏️ Edit',
        search_ph: '🔍 Search by barcode or item name…',
      },
      sell: {
        menu_title: 'Sell — what would you like to do?',
        quick: '⚡ Quick sale (one item)',
        none: 'No matching item found.',
        sold: (qty, name, total) => `✅ Sold ${qty} × ${name} for ${total} total.`,
        low_stock: (n) => `⚠️ Only ${n} left in stock!`,
        not_enough_stock: (n) => `Only ${n} in stock.`,
        print_barcode: '🖨️ Print barcode',
        barcode_title: (name) => `Barcode for ${name}:`,
        print_btn: '🖨️ Print',
        form: {
          identifier: 'Product name or barcode', identifier_ph: 'e.g. Coke 0.5 L',
          quantity: 'Quantity',
          save: 'Sell', saving: 'Selling...',
          payment_method_label: 'Payment method', payment_cash: 'Cash', payment_card: 'Card',
          card_not_configured: 'Card payments are not set up (STRIPE_SECRET_KEY missing).',
          card_reader_discovering: 'Looking for the card reader...',
          card_reader_connecting: 'Connecting to the card reader...',
          card_creating_intent: 'Preparing the payment...',
          card_present_card: 'Please present or insert the card on the reader...',
          card_processing: 'Processing payment...',
          card_error_prefix: 'Error:',
        },
      },
    },
    ar: {
      title: 'المساعد',
      launcher_label: 'فتح المساعد',
      close_label: 'إغلاق',
      input_placeholder: 'اكتب رسالة أو استخدم الميكروفون…',
      listening_placeholder: '🎙️ أستمع الآن…',
      greeting: '👋 مرحبًا! أنا مساعدك. ماذا تريد أن تفعل؟',
      ask_next: 'ماذا تريد أن تفعل بعد ذلك؟',
      not_understood: '🤔 لم أفهم ذلك تمامًا. الرجاء اختيار خيار:',
      generic_error: '❌ حدث خطأ ما.',
      connection_error: 'تعذّر الاتصال بالخادم.',
      history_title: '📜 السجل',
      history_none: 'لا توجد محادثات سابقة بعد.',
      history_load: 'فتح',
      history_delete: 'حذف',
      history_confirm_delete: 'هل تريد فعلاً حذف هذه المحادثة نهائيًا؟',
      history_untitled: 'محادثة جديدة',
      new_chat_started: '✨ تم بدء محادثة جديدة.',
      widgets: {
        sales_today: 'مبيعات اليوم', profit_today: 'ربح اليوم', sales_month: 'مبيعات الشهر',
        kasse_balance: 'رصيد الصندوق', debts_open: 'ديون مفتوحة', low_stock: 'مخزون منخفض',
        date: 'التاريخ', user: 'البائع', items: 'العناصر', total: 'الإجمالي',
        client: 'العميل', amount: 'المبلغ', description: 'الوصف',
        product: 'المنتج', qty: 'الكمية',
      },
      confirm_prompt: 'يرجى التأكيد:',
      confirm_yes: '✅ نعم، تنفيذ',
      confirm_cancel: '✖ إلغاء',
      confirm_cancelled: 'تم الإلغاء — لم يتم تغيير أي شيء.',
      mic_unsupported: '🎙️ الإدخال الصوتي غير مدعوم في هذا المتصفح. جرّب Chrome أو استمر بالكتابة.',
      mic_permission_denied: '🎙️ تم رفض الوصول إلى الميكروفون. يرجى السماح به في إعدادات المتصفح/الموقع.',
      mic_error: '🎙️ لم يعمل الإدخال الصوتي. يرجى المحاولة مرة أخرى أو الكتابة بدلاً من ذلك.',
      mic_too_short: '🎙️ كانت مدة التسجيل قصيرة جدًا أو لم أسمع شيئًا. يرجى التحدث لفترة أطول قليلاً.',
      back: '⬅ القائمة الرئيسية',
      menu: {
        report: '📊 تقرير',
        debts: '💸 الديون',
        factures: '🧾 الفواتير',
        stock: '📦 المخزون',
        kasse: '💰 الصندوق',
        items: '📦 الأصناف',
        sellers: '👤 البائعون',
        salary: '💵 الرواتب',
        orders: '🛒 الشراء',
        sales: '🧾 مبيعاتي',
        sell: '💳 بيع',
      },
      report: {
        which: 'ما التقرير الذي تريد رؤيته؟',
        overview: '📈 نظرة عامة',
        title: '📊 نظرة عامة',
        sales_today: 'مبيعات اليوم',
        profit_today: 'ربح اليوم',
        sales_month: 'مبيعات هذا الشهر',
        kasse_balance: 'رصيد الصندوق',
        debts_open: 'الديون المفتوحة',
        factures_unpaid: 'الفواتير غير المدفوعة',
        low_stock: 'مخزون منخفض',
        items: 'صنف',
      },
      debts: {
        menu_title: 'الديون — ماذا تريد أن تفعل؟',
        view: '📋 عرض الديون المفتوحة',
        add: '➕ إضافة دين جديد',
        none: '🎉 لا توجد ديون مفتوحة.',
        count: (n) => `يوجد <strong>${n}</strong> دين (ديون) مفتوحة:`,
        mark_paid: '✅ وضع علامة "مدفوع"',
        marked: '✔ تم',
        added: (name, amount) => `✅ تمت إضافة دين بقيمة ${amount} لـ ${name}.`,
        paid_voice: (name) => `✅ تم وضع علامة "مدفوع" على دين ${name}.`,
        not_found: (name) => `❌ لم أجد دينًا مفتوحًا باسم "${name}".`,
        need_info: '⚠️ الرجاء ذكر الاسم والمبلغ، مثال: "إضافة دين أحمد 50".',
        add_more: '➕ إضافة آخر',
        view_clients: '👥 عرض العملاء',
        delete_all: '🗑 حذف كل الديون',
        confirm_delete_all: '⚠️ هل تريد حقًا حذف <strong>كل</strong> ديون جميع العملاء نهائيًا؟ لا يمكن التراجع عن هذا الإجراء.',
        deleted_all: (n) => `🗑 تم حذف ${n} دين (ديون).`,
        edit_btn: '✏️ تعديل',
        edited: (name) => `✅ تم تحديث دين ${name}.`,
        reference_label: 'مرجع',
        clients: {
          none: '🎉 لا يوجد عملاء مسجلون بعد.',
          list_title: (n) => `<strong>${n}</strong> عميل (عملاء):`,
          history_title: (name) => `ديون <strong>${escapeHtml(name)}</strong>:`,
          no_debts: 'لا توجد ديون مسجلة لهذا العميل.',
          paid_label: 'مدفوع',
          open_label: 'مفتوح',
          back_to_clients: '⬅ العودة لقائمة العملاء',
        },
        form: {
          client: 'العميل', client_ph: 'اسم العميل',
          amount: 'المبلغ (€)',
          phone: 'الهاتف', phone_ph: 'اختياري',
          description: 'الوصف', description_ph: 'مثال: رصيد متجر',
          reference: 'الرقم المرجعي',
          save: 'حفظ', saving: 'جارٍ الحفظ...',
          payment_method_label: 'طريقة الدفع', payment_cash: 'نقدًا', payment_card: 'بطاقة',
          card_not_configured: 'الدفع بالبطاقة غير مُعد (STRIPE_SECRET_KEY مفقود).',
          card_reader_discovering: 'جارٍ البحث عن قارئ البطاقة...',
          card_reader_connecting: 'جارٍ الاتصال بقارئ البطاقة...',
          card_creating_intent: 'جارٍ تجهيز الدفع...',
          card_present_card: 'الرجاء تمرير أو إدخال البطاقة في القارئ...',
          card_processing: 'جارٍ معالجة الدفع...',
          card_error_prefix: 'خطأ:',
        },
      },
      factures: {
        menu_title: 'الفواتير — ماذا تريد أن تفعل؟',
        view: '📋 عرض الفواتير غير المدفوعة',
        add: '➕ إضافة فاتورة جديدة',
        none: '🎉 لا توجد فواتير غير مدفوعة.',
        count: (n) => `يوجد <strong>${n}</strong> فاتورة (فواتير) غير مدفوعة:`,
        mark_paid: '✅ وضع علامة "مدفوع"',
        marked: '✔ تم',
        due: 'الاستحقاق',
        add_more: '➕ إضافة أخرى',
        edit_btn: '✏️ تعديل',
        delete_btn: '🗑 حذف',
        edited: (name) => `✅ تم تحديث فاتورة ${name}.`,
        confirm_delete: (name) => `⚠️ هل تريد حذف فاتورة <strong>${name}</strong> نهائيًا؟`,
        deleted: '🗑 تم حذف الفاتورة.',
        search_ph: '🔎 البحث حسب الجهة المصدرة…',
        load_more: (n) => `⬇️ تحميل المزيد (${n} متبقية)`,
        showing: (shown, total) => `${shown} من ${total}`,
        types: {
          ebay: 'إيباي', electricity: 'كهرباء', water: 'ماء',
          internet: 'إنترنت', phone: 'هاتف', supplier: 'مورّد',
          rent: 'إيجار', other: 'أخرى',
        },
        form: {
          type: 'النوع', issuer: 'الجهة المصدرة', issuer_ph: 'مثال: eBay، EDF، Vodafone',
          amount: 'المبلغ (€)', due_date: 'تاريخ الاستحقاق',
          save: 'حفظ', saving: 'جارٍ الحفظ...',
          edit_title: 'تعديل الفاتورة', saving_edit: 'جارٍ التحديث...',
        },
      },
      stock: {
        menu_title: 'المخزون — ماذا تريد أن تفعل؟',
        view: '⚠️ عرض المخزون المنخفض',
        none: '✅ لا توجد أصناف بمخزون منخفض.',
        warning: '⚠️ الأصناف التالية لديها مخزون منخفض:',
      },
      kasse: {
        menu_title: 'الصندوق — ماذا تريد أن تفعل؟',
        view: '💰 عرض رصيد الصندوق اليوم',
        history: '📜 عرض السجل',
        history_title: (n) => `آخر <strong>${n}</strong> عملية في الصندوق:`,
        history_none: 'لم يتم العثور على عمليات في الصندوق.',
        add: '➕ تسجيل إيداع/سحب',
        title: '💰 الصندوق اليوم',
        balance: 'رصيد الصندوق',
        sales_today: 'مبيعات اليوم',
        purchases_today: 'مشتريات اليوم',
        deposits_today: 'الإيداعات اليوم',
        withdrawals_today: 'السحوبات اليوم',
        types: { einzahlung: 'إيداع', auszahlung: 'سحب' },
        form: { type: 'النوع', amount: 'المبلغ (€)', description: 'الوصف', description_ph: 'مثال: صندوق الفكة', save: 'حفظ', saving: 'جارٍ الحفظ...' },
        booked: (typ, amount) => `✅ تم تسجيل ${typ} بقيمة ${amount}.`,
      },
      items: {
        menu_title: 'الأصناف — ماذا تريد أن تفعل؟',
        view: '📋 عرض الأصناف',
        add: '➕ إضافة صنف جديد',
        low_stock: '⚠️ عرض المخزون المنخفض',
        none: 'لم يتم العثور على أصناف.',
        list_title: (n) => `تم العثور على <strong>${n}</strong> صنف/أصناف:`,
        search_ph: '🔍 ابحث بالباركود أو الاسم…',
        added: (name, code) => `✅ تمت إضافة الصنف "${name}" (باركود ${code}).`,
        add_more: '➕ إضافة آخر',
        edit_btn: (name) => `✏️ تعديل ${name}`,
        delete_btn: '🗑 حذف',
        confirm_delete: (name) => `⚠️ هل تريد حقًا حذف الصنف <strong>${name}</strong> نهائيًا؟`,
        deleted: '🗑 تم حذف الصنف.',
        form: {
          name: 'اسم المنتج', name_ph: 'مثال: كولا 0.5 لتر',
          quantity: 'الكمية', purchase_price: 'سعر الشراء (€)', selling_price: 'سعر البيع (€)',
          barcode: 'الباركود', barcode_ph: 'اتركه فارغًا للتوليد التلقائي',
          save: 'حفظ', saving: 'جارٍ الحفظ...',
        },
      },
      sellers: {
        menu_title: 'البائعون — ماذا تريد أن تفعل؟',
        view: '📋 عرض البائعين',
        add: '➕ إضافة بائع جديد',
        none: 'لم يتم العثور على بائعين.',
        list_title: (n) => `<strong>${n}</strong> بائع/بائعين:`,
        added: (name) => `✅ تمت إضافة البائع "${name}".`,
        add_more: '➕ إضافة آخر',
        edit_btn: (name) => `✏️ تعديل ${name}`,
        delete_btn: '🗑 حذف',
        confirm_delete: (name) => `⚠️ هل تريد حقًا حذف البائع <strong>${name}</strong> نهائيًا؟`,
        deleted: '🗑 تم حذف البائع.',
        active: 'نشط', inactive: 'غير نشط',
        form: {
          username: 'اسم المستخدم', password: 'كلمة المرور', salary: 'الراتب (€)',
          save: 'حفظ', saving: 'جارٍ الحفظ...',
        },
      },
      salary: {
        menu_title: 'الرواتب — ماذا تريد أن تفعل؟',
        view: '📋 عرض آخر الدفعات',
        add: '➕ دفع راتب',
        none: 'لم يتم العثور على دفعات.',
        list_title: (n) => `آخر <strong>${n}</strong> دفعة/دفعات:`,
        paid: (name, amount) => `✅ تم دفع ${amount} لـ ${name}.`,
        add_more: '➕ دفعة أخرى',
        select_ph: 'الرجاء الاختيار…',
        no_employees: 'لم يتم العثور على بائعين. الرجاء إضافة بائع أولاً.',
        form: {
          employee: 'الموظف', amount: 'المبلغ (€)', source: 'المصدر',
          save: 'دفع', saving: 'جارٍ الحفظ...',
        },
      },
      scan: {
        button: '📷 تصوير الفاتورة أو PDF',
        uploading: '📷 جارٍ القراءة (OCR/PDF)…',
        failed: '❌ تعذّرت قراءة الملف. يرجى تعبئة الحقول يدويًا أو تجربة صورة أوضح.',
        found: '📸 هذه هي البيانات التي تم التعرف عليها — يرجى المراجعة والحفظ:',
        check_amount: '⚠️ يرجى التحقق من المبلغ — لم أجد تسمية واضحة مثل "الإجمالي"، لذا خمّنت أكبر رقم في الصفحة.',
      },
      orders: {
        menu_title: 'الشراء — ماذا تريد أن تفعل؟',
        view: '📋 عرض آخر طلبات الشراء',
        add: '➕ تسجيل طلب شراء جديد',
        none: 'لم يتم العثور على طلبات شراء.',
        list_title: (n) => `آخر <strong>${n}</strong> طلب/طلبات شراء:`,
        added: (name, qty, total) => `✅ تم حفظ طلب "${name}" (${qty} قطعة، ${total}).`,
        add_more: '➕ طلب آخر',
        edit_btn: (n) => `✏️ تعديل الطلب #${n}`,
        delete_btn: '🗑 حذف',
        confirm_delete: (n) => `⚠️ هل تريد فعلاً حذف الطلب <strong>#${n}</strong> نهائيًا؟`,
        deleted: '🗑 تم حذف الطلب.',
        search_ph: '🔍 ابحث بالباركود أو اسم المنتج…',
        form: {
          product_name: 'اسم المنتج', product_name_ph: 'مثال: كولا 0.5 لتر',
          price: 'سعر الشراء (€)', selling_price: 'سعر البيع (€)',
          quantity: 'الكمية', ref_number: 'رقم مرجعي', ref_number_ph: 'اختياري',
          save: 'حفظ الطلب', saving: 'جارٍ الحفظ...',
          payment_method_label: 'طريقة الدفع', payment_cash: 'نقدًا', payment_card: 'بطاقة',
          card_not_configured: 'الدفع بالبطاقة غير مُعدّ بعد (STRIPE_SECRET_KEY مفقود).',
          card_reader_discovering: 'جارٍ البحث عن قارئ البطاقة...',
          card_reader_connecting: 'جارٍ الاتصال بقارئ البطاقة...',
          card_creating_intent: 'جارٍ تجهيز عملية الدفع...',
          card_present_card: 'يرجى تمرير أو إدخال البطاقة في القارئ...',
          card_processing: 'جارٍ معالجة الدفع...',
          card_error_prefix: 'خطأ:',
        },
      },
      sales: {
        menu_title: 'مبيعاتي — ماذا تريد أن تفعل؟',
        view: '📋 عرض آخر المبيعات',
        none: 'لم يتم العثور على مبيعات.',
        list_title: (n) => `آخر <strong>${n}</strong> عملية بيع:`,
        row: (n, total, items) => `#${n} — ${total} (${items} صنف)`,
        delete_btn: '🗑 حذف',
        delete_confirm: (n) => `هل تريد حذف عملية البيع رقم ${n}؟ لن تتم إعادة المخزون تلقائيًا.`,
        deleted: '✅ تم حذف عملية البيع.',
        edit_btn: '✏️ تعديل',
        search_ph: '🔍 ابحث بالباركود أو اسم الصنف…',
      },
      sell: {
        menu_title: 'بيع — ماذا تريد أن تفعل؟',
        quick: '⚡ بيع سريع (صنف واحد)',
        none: 'لم يتم العثور على صنف مطابق.',
        sold: (qty, name, total) => `✅ تم بيع ${qty} × ${name} بإجمالي ${total}.`,
        low_stock: (n) => `⚠️ تبقّى ${n} فقط في المخزون!`,
        not_enough_stock: (n) => `متوفر ${n} فقط.`,
        print_barcode: '🖨️ طباعة الباركود',
        barcode_title: (name) => `الباركود لـ ${name}:`,
        print_btn: '🖨️ طباعة',
        form: {
          identifier: 'اسم المنتج أو الباركود', identifier_ph: 'مثال: كولا 0.5 لتر',
          quantity: 'الكمية',
          save: 'بيع', saving: 'جارٍ البيع...',
          payment_method_label: 'طريقة الدفع', payment_cash: 'نقدًا', payment_card: 'بطاقة',
          card_not_configured: 'الدفع بالبطاقة غير مُعد (STRIPE_SECRET_KEY مفقود).',
          card_reader_discovering: 'جارٍ البحث عن قارئ البطاقة...',
          card_reader_connecting: 'جارٍ الاتصال بقارئ البطاقة...',
          card_creating_intent: 'جارٍ تجهيز الدفع...',
          card_present_card: 'الرجاء تمرير أو إدخال البطاقة في القارئ...',
          card_processing: 'جارٍ معالجة الدفع...',
          card_error_prefix: 'خطأ:',
        },
      },
    },
  };

  // Keyword sets used by the free-text / voice NLU parser.
  const KEYWORDS = {
    de: {
      report: ['bericht', 'übersicht', 'zusammenfassung', 'report'],
      debts: ['schuld', 'schulden'],
      factures: ['rechnung', 'rechnungen'],
      stock: ['lager', 'lagerbestand', 'bestand'],
      kasse: ['kasse', 'kassenstand'],
      items: ['artikel', 'produkt', 'produkte', 'ware'],
      sellers: ['verkäufer', 'mitarbeiter', 'personal'],
      salary: ['gehalt', 'gehälter', 'lohn'],
      orders: ['einkauf', 'einkaufen', 'bestellung', 'bestellungen', 'lieferung'],
      sales: ['verkäufe', 'meine verkäufe', 'umsatz'],
      sell: ['verkaufen', 'verkauf'],
      scan: ['foto', 'scannen', 'fotografieren', 'bild', 'kamera'],
      add: ['hinzufügen', 'hinzu', 'neu', 'anlegen', 'erstellen'],
      pay: ['bezahlt', 'bezahlen', 'beglichen', 'zahlen', 'auszahlen'],
      show: ['zeigen', 'anzeigen', 'liste', 'offene', 'wieviel', 'wie viel'],
      back: ['zurück', 'hauptmenü', 'menü'],
    },
    en: {
      report: ['report', 'summary', 'overview'],
      debts: ['debt', 'debts'],
      factures: ['invoice', 'invoices', 'bill', 'bills'],
      stock: ['stock', 'inventory'],
      kasse: ['cash', 'register', 'till', 'kasse'],
      items: ['item', 'items', 'product', 'products'],
      sellers: ['seller', 'sellers', 'employee', 'staff'],
      salary: ['salary', 'wage', 'wages', 'payroll'],
      orders: ['purchase', 'purchasing', 'order', 'orders', 'buying'],
      sales: ['my sales', 'sales history'],
      sell: ['sell', 'selling', 'sale'],
      scan: ['photo', 'scan', 'picture', 'camera', 'image'],
      add: ['add', 'new', 'create'],
      pay: ['paid', 'pay'],
      show: ['show', 'list', 'view', 'open', 'how much'],
      back: ['back', 'menu'],
    },
    ar: {
      report: ['تقرير', 'ملخص', 'نظرة'],
      debts: ['دين', 'ديون'],
      factures: ['فاتورة', 'فواتير'],
      stock: ['مخزون'],
      kasse: ['صندوق', 'كاش', 'نقد'],
      items: ['صنف', 'أصناف', 'منتج', 'منتجات'],
      sellers: ['بائع', 'بائعين', 'موظف', 'موظفين'],
      salary: ['راتب', 'رواتب', 'أجر'],
      orders: ['شراء', 'طلب شراء', 'طلبات شراء', 'توريد'],
      sales: ['مبيعاتي', 'مبيعات'],
      sell: ['بيع', 'بيع منتج'],
      scan: ['صورة', 'تصوير', 'مسح', 'كاميرا'],
      add: ['إضافة', 'اضف', 'أضف', 'جديد'],
      pay: ['دفع', 'سدد', 'سداد', 'مدفوع'],
      show: ['عرض', 'اظهار', 'قائمة'],
      back: ['رجوع', 'القائمة'],
    },
  };

  // Levenshtein distance (capped) so typos like "rechnnug" or "invoic" still
  // match — this is the bulk of what makes free-text feel "smarter" without
  // calling any external AI.
  function levenshtein(a, b, max = 2) {
    if (Math.abs(a.length - b.length) > max) return max + 1;
    const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
    for (let j = 0; j <= b.length; j++) dp[0][j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) {
        dp[i][j] = a[i - 1] === b[j - 1]
          ? dp[i - 1][j - 1]
          : 1 + Math.min(dp[i - 1][j - 1], dp[i - 1][j], dp[i][j - 1]);
      }
    }
    return dp[a.length][b.length];
  }

  // True if any keyword appears verbatim OR any word in the text is a close
  // typo (edit distance <= 1 for short words, <= 2 for longer ones) of a keyword.
  function fuzzyContainsAny(text, words) {
    if (words.some((w) => text.includes(w))) return true;
    const tokens = text.split(/\s+/).filter(Boolean);
    return words.some((w) => {
      if (w.length < 4) return false; // too short to fuzzy-match safely
      const tolerance = w.length <= 6 ? 1 : 2;
      return tokens.some((tok) => Math.abs(tok.length - w.length) <= tolerance && levenshtein(tok, w, tolerance) <= tolerance);
    });
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const SUPPORTED_LANGS = ['de', 'en', 'ar'];
  const siteLang = SUPPORTED_LANGS.includes(panel.dataset.siteLang) ? panel.dataset.siteLang : 'de';

  const state = {
    lang: localStorage.getItem('assistantLang') || siteLang,
    // True only once the person explicitly picks a flag chip. state.lang
    // itself is set from the start (from a previous session or the site's
    // language) — that's a fine *display* default, but it is NOT a
    // deliberate signal about which language they're about to speak. Only
    // forceLangForVoice being true means "trust this for the Whisper hint";
    // otherwise we let Whisper auto-detect from the audio itself.
    forceLangForVoice: false,
    lastInputWasVoice: false,
    currentCategory: 'main',
    // Which GPT-style conversation is currently active (see the History
    // sidebar) — null until ensureHistoryLoaded() resolves it, either from
    // localStorage or from the server's "most recent conversation" fallback.
    conversationId: localStorage.getItem('assistantConversationId') || null,
  };

  function t(path) {
    const parts = path.split('.');
    let node = I18N[state.lang];
    for (const p of parts) node = node ? node[p] : undefined;
    return node !== undefined ? node : path;
  }

  let opened = false;
  let recognition = null;
  let recognizing = false;

  // ---------------------------------------------------------------------
  // Language handling — no manual switch buttons: the chat detects
  // whichever language the person writes/speaks in (see detectLanguage
  // near the NLU section) and calls applyLanguage() itself.
  // ---------------------------------------------------------------------
  function applyLanguage(lang) {
    if (!SUPPORTED_LANGS.includes(lang)) return;
    state.lang = lang;
    localStorage.setItem('assistantLang', lang);
    panel.setAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
    panel.setAttribute('lang', SPEECH_LANG[lang].split('-')[0]);
    if (titleEl) titleEl.textContent = t('title');
    launcher.setAttribute('aria-label', t('launcher_label'));
    closeBtn.setAttribute('aria-label', t('close_label'));
    if (textInput) textInput.placeholder = t('input_placeholder');
    if (recognition) recognition.lang = SPEECH_LANG[lang];
    if (headerLangSwitch) {
      headerLangSwitch.querySelectorAll('.assistant-lang-chip').forEach((chip) => {
        chip.classList.toggle('active', chip.dataset.lang === lang);
      });
    }
  }

  // The flag chips in the header (DE/GB/SA) let someone force the chat's
  // language instead of waiting for auto-detection — important for voice
  // input especially, since the language picked here is also sent to the
  // server as the transcription hint. Without a click handler these were
  // pure decoration.
  if (headerLangSwitch) {
    headerLangSwitch.querySelectorAll('.assistant-lang-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        applyLanguage(chip.dataset.lang);
        state.forceLangForVoice = true;
        persistState();
      });
    });
  }

  function updateBackButtonVisibility() {
    if (backBtn) backBtn.style.display = state.currentCategory === 'main' ? 'none' : '';
  }

  // ---------------------------------------------------------------------
  // Persistence — the conversation (and which screen is open) survives
  // navigating to a different page of the app, instead of resetting.
  // ---------------------------------------------------------------------
  function persistState() {
    try {
      const children = Array.from(body.children);
      if (children.length > MAX_STORED_MESSAGES) {
        children.slice(0, children.length - MAX_STORED_MESSAGES).forEach((el) => el.remove());
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        lang: state.lang,
        forceLangForVoice: state.forceLangForVoice,
        category: state.currentCategory,
        open: panel.classList.contains('open'),
        html: body.innerHTML,
      }));
      if (state.conversationId) localStorage.setItem('assistantConversationId', state.conversationId);
    } catch (e) {
      // Storage full/unavailable — the chat still works, it just won't persist.
    }
  }

  function restoreState() {
    let saved = null;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) saved = JSON.parse(raw);
    } catch (e) { saved = null; }
    if (!saved || !saved.html) return;

    if (SUPPORTED_LANGS.includes(saved.lang)) applyLanguage(saved.lang);
    state.forceLangForVoice = !!saved.forceLangForVoice;

    // Replay the saved transcript as static bubbles. Old option buttons /
    // inline forms lose their click handlers across a page reload, so
    // they're dropped here and a fresh, working set is rendered below
    // instead of leaving dead buttons in the history.
    const temp = document.createElement('div');
    temp.innerHTML = saved.html;
    temp.querySelectorAll('.assistant-options, .assistant-typing, .assistant-inline-form').forEach((el) => el.remove());
    body.innerHTML = temp.innerHTML;
    scrollToBottom();

    opened = true;
    const category = CATEGORY_ENTRY[saved.category] ? saved.category : 'main';

    if (saved.open) {
      panel.classList.add('open');
      document.body.classList.toggle('assistant-lock-scroll', window.innerWidth <= 640);
      const ping = launcher.querySelector('.assistant-ping');
      if (ping) ping.remove();
    }

    // Resume with a fresh, interactive render of whichever screen was active.
    CATEGORY_ENTRY[category]();
  }

  function setOpenClass(isOpen) {
    panel.classList.toggle('open', isOpen);
    document.body.classList.toggle('assistant-lock-scroll', isOpen && window.innerWidth <= 640);
    persistState();
  }

  launcher.addEventListener('click', () => {
    const willOpen = !panel.classList.contains('open');
    setOpenClass(willOpen);
    const ping = launcher.querySelector('.assistant-ping');
    if (ping) ping.remove();
    if (willOpen && !opened) {
      opened = true;
      showMainMenu(true);
    }
  });
  closeBtn.addEventListener('click', () => setOpenClass(false));
  if (backBtn) backBtn.addEventListener('click', () => showMainMenu());

  // Set initial static labels before the panel is first opened.
  applyLanguage(state.lang);

  // ---------------------------------------------------------------------
  // Rendering helpers
  // ---------------------------------------------------------------------
  function scrollToBottom() {
    body.scrollTop = body.scrollHeight;
  }

  function addBotMessage(html, opts) {
    const div = document.createElement('div');
    div.className = 'assistant-msg bot';
    div.innerHTML = html;
    body.appendChild(div);
    scrollToBottom();
    // Speak by default when the turn started with voice input — every call
    // site that wants to opt OUT already passes { speak: false } (error/
    // status messages), so the default here should be "speak", not
    // "require an opts object to exist at all".
    if ((!opts || opts.speak !== false) && state.lastInputWasVoice) {
      speak(div.textContent);
    }
    persistState();
    return div;
  }

  function addUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'assistant-msg user';
    div.textContent = text;
    body.appendChild(div);
    scrollToBottom();
    persistState();
  }

  function showTyping() {
    const div = document.createElement('div');
    div.className = 'assistant-typing';
    div.innerHTML = '<span class="assistant-typing-orb"></span><span></span><span></span><span></span>';
    body.appendChild(div);
    scrollToBottom();
    return div;
  }

  // ---------------------------------------------------------------------
  // Rich widgets: the chat isn't limited to text bubbles. When the AI's
  // tool call returned something structured (a product list, a summary,
  // recent sales...), the backend attaches it as `widget` on the chat
  // response (see WIDGETABLE_TOOLS / last_widget in app.py). This renders
  // that as a proper card grid / table / dashboard strip / bar chart
  // instead of the AI having to describe it in prose.
  function addWidgetContainer() {
    const div = document.createElement('div');
    div.className = 'assistant-msg bot assistant-widget';
    body.appendChild(div);
    scrollToBottom();
    persistState();
    return div;
  }

  function renderWidget(widget) {
    if (!widget || !widget.type || !widget.data) return;
    const container = addWidgetContainer();
    try {
      switch (widget.type) {
        case 'get_summary': return renderSummaryWidget(container, widget.data);
        case 'list_items': return renderItemCardsWidget(container, widget.data.items || []);
        case 'list_recent_sales': return renderSalesTableWidget(container, widget.data.sales || []);
        case 'list_open_debts': return renderDebtsTableWidget(container, widget.data.debts || []);
        case 'list_low_stock': return renderLowStockWidget(container, widget.data.items || []);
        case 'list_recent_orders': return renderOrdersTableWidget(container, widget.data.orders || []);
        default: container.remove();
      }
    } catch (e) {
      container.remove(); // never let a rendering bug break the chat itself
    }
  }

  // Dashboard widget: a strip of stat cards + one small bar chart, built
  // from get_summary's numbers (today's sales/profit/kasse/debts, etc.).
  function renderSummaryWidget(container, d) {
    const stats = [
      { label: t('widgets.sales_today'), value: fmtMoney(d.sales_today) },
      { label: t('widgets.profit_today'), value: fmtMoney(d.profit_today) },
      { label: t('widgets.sales_month'), value: fmtMoney(d.sales_month) },
      { label: t('widgets.kasse_balance'), value: fmtMoney(d.kasse_balance) },
      { label: t('widgets.debts_open'), value: `${d.debts_open_count} · ${fmtMoney(d.debts_open_total)}` },
      { label: t('widgets.low_stock'), value: String(d.low_stock_count) },
    ];
    const maxVal = Math.max(d.sales_today || 0, d.profit_today || 0, 1);
    container.innerHTML = `
      <div class="assistant-stat-grid">
        ${stats.map((s) => `
          <div class="assistant-stat-card">
            <div class="assistant-stat-value">${escapeHtml(s.value)}</div>
            <div class="assistant-stat-label">${escapeHtml(s.label)}</div>
          </div>`).join('')}
      </div>
      <div class="assistant-bar-chart" role="img" aria-label="${t('widgets.sales_today')} / ${t('widgets.profit_today')}">
        <div class="assistant-bar-row">
          <span class="assistant-bar-label">${t('widgets.sales_today')}</span>
          <div class="assistant-bar-track"><div class="assistant-bar-fill" style="width:${Math.round(100 * (d.sales_today || 0) / maxVal)}%"></div></div>
          <span class="assistant-bar-val">${fmtMoney(d.sales_today)}</span>
        </div>
        <div class="assistant-bar-row">
          <span class="assistant-bar-label">${t('widgets.profit_today')}</span>
          <div class="assistant-bar-track"><div class="assistant-bar-fill profit" style="width:${Math.round(100 * (d.profit_today || 0) / maxVal)}%"></div></div>
          <span class="assistant-bar-val">${fmtMoney(d.profit_today)}</span>
        </div>
      </div>`;
  }

  // Product cards: used for list_items results (a search / "what iPhones do
  // we have" type lookup) — this is the "which iPhone?" step of the sell
  // conversation as well as a plain stock lookup.
  function renderItemCardsWidget(container, items) {
    if (!items.length) { container.remove(); return; }
    container.innerHTML = `<div class="assistant-card-grid">
      ${items.map((i) => `
        <div class="assistant-product-card">
          <div class="assistant-product-name">${escapeHtml(i.product_name || '')}</div>
          <div class="assistant-product-meta">
            <span class="assistant-product-price">${fmtMoney(i.selling_price)}</span>
            <span class="assistant-product-qty ${i.quantity <= 5 ? 'low' : ''}">${i.quantity}×</span>
          </div>
        </div>`).join('')}
    </div>`;
  }

  function renderLowStockWidget(container, items) {
    if (!items.length) { container.remove(); return; }
    container.innerHTML = `<div class="assistant-card-grid">
      ${items.map((i) => `
        <div class="assistant-product-card low">
          <div class="assistant-product-name">${escapeHtml(i.message || i.barcode || '')}</div>
        </div>`).join('')}
    </div>`;
  }

  function renderSalesTableWidget(container, sales) {
    if (!sales.length) { container.remove(); return; }
    container.innerHTML = `<div class="assistant-table-wrap"><table class="assistant-table">
      <thead><tr><th>${t('widgets.date')}</th><th>${t('widgets.user')}</th><th>${t('widgets.items')}</th><th>${t('widgets.total')}</th></tr></thead>
      <tbody>
        ${sales.map((s) => `<tr>
          <td>${escapeHtml(s.date || '')}</td>
          <td>${escapeHtml(s.user || '')}</td>
          <td>${s.items_count}</td>
          <td>${fmtMoney(s.total)}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
  }

  function renderDebtsTableWidget(container, debts) {
    if (!debts.length) { container.remove(); return; }
    container.innerHTML = `<div class="assistant-table-wrap"><table class="assistant-table">
      <thead><tr><th>${t('widgets.client')}</th><th>${t('widgets.amount')}</th><th>${t('widgets.description')}</th></tr></thead>
      <tbody>
        ${debts.map((d) => `<tr>
          <td>${escapeHtml(d.client_name || '')}</td>
          <td>${fmtMoney(d.amount)}</td>
          <td>${escapeHtml(d.description || '')}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
  }

  function renderOrdersTableWidget(container, orders) {
    if (!orders.length) { container.remove(); return; }
    container.innerHTML = `<div class="assistant-table-wrap"><table class="assistant-table">
      <thead><tr><th>${t('widgets.product')}</th><th>${t('widgets.qty')}</th><th>${t('widgets.total')}</th><th>${t('widgets.date')}</th></tr></thead>
      <tbody>
        ${orders.map((o) => `<tr>
          <td>${escapeHtml(o.product_name || '')}</td>
          <td>${o.quantity}</td>
          <td>${fmtMoney(o.total_price)}</td>
          <td>${escapeHtml(String(o.date || ''))}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
  }

  // Splits a "📊 Bericht" style label into { icon: '📊', text: 'Bericht' }.
  // Every menu label across all three languages already starts with an
  // emoji (see I18N above), so this needs no extra data — it just gives
  // the existing emoji a proper visual home instead of sitting inline
  // with the text.
  function splitIconLabel(label) {
    const match = /^(\p{Extended_Pictographic}\uFE0F?)\s*(.*)$/u.exec(label || '');
    if (match && match[2]) return { icon: match[1], text: match[2] };
    return { icon: null, text: label };
  }

  function addOptions(options, opts) {
    const wrap = document.createElement('div');
    wrap.className = 'assistant-options' + (opts && opts.tiles ? ' assistant-options-tiles' : '');
    options.forEach((opt) => {
      const btn = document.createElement('button');
      const isTile = !!(opts && opts.tiles);
      btn.className = (isTile ? 'assistant-option-tile' : 'assistant-option-btn') + (opt.variant ? ' ' + opt.variant : '');
      if (isTile) {
        const { icon, text } = splitIconLabel(opt.label);
        btn.innerHTML = (icon ? `<span class="tile-icon">${icon}</span>` : '') + `<span class="tile-text">${text}</span>`;
      } else {
        btn.textContent = opt.label;
      }
      btn.addEventListener('click', () => {
        wrap.remove();
        addUserMessage(opt.label);
        state.lastInputWasVoice = false;
        opt.action();
      });
      wrap.appendChild(btn);
    });
    body.appendChild(wrap);
    scrollToBottom();
  }

  // Ask the person to confirm before any action that writes to the
  // database (add/pay/book/...). Shows the action summary plus a
  // Yes/Cancel choice and resolves to true only if they pick "Yes".
  function confirmAction(summaryHtml) {
    return new Promise((resolve) => {
      addBotMessage(summaryHtml, { speak: false });
      const wrap = document.createElement('div');
      wrap.className = 'assistant-options';

      const yesBtn = document.createElement('button');
      yesBtn.className = 'assistant-option-btn gold';
      yesBtn.textContent = t('confirm_yes');

      const cancelBtn = document.createElement('button');
      cancelBtn.className = 'assistant-option-btn';
      cancelBtn.textContent = t('confirm_cancel');

      const settle = (value, label) => {
        wrap.remove();
        addUserMessage(label);
        state.lastInputWasVoice = false;
        resolve(value);
      };
      yesBtn.addEventListener('click', () => settle(true, t('confirm_yes')));
      cancelBtn.addEventListener('click', () => settle(false, t('confirm_cancel')));

      wrap.appendChild(yesBtn);
      wrap.appendChild(cancelBtn);
      body.appendChild(wrap);
      scrollToBottom();
    });
  }

  // Ask cash vs. card, and for card, connect to the physical Stripe
  // Terminal (TPE) reader and wait for a real, captured payment — the same
  // device/flow the purchase-order form already uses. Never resolves
  // "card" without a genuine payment_intent_id from the reader, since the
  // server independently re-verifies it against Stripe before accepting
  // the payment (a chat message alone is never proof that money moved).
  // `formNs` picks which i18n form block (e.g. debts.form / sell.form)
  // supplies the button labels and status text.
  function collectPaymentMethod(amount, reference, formNs) {
    const f = I18N[state.lang][formNs].form;
    return new Promise((resolve) => {
      const wrap = document.createElement('div');
      wrap.className = 'assistant-inline-form';

      const label = document.createElement('div');
      label.className = 'assistant-inline-hint';
      label.textContent = f.payment_method_label;
      wrap.appendChild(label);

      const btnRow = document.createElement('div');
      btnRow.className = 'row2';
      btnRow.style.gap = '0.5rem';

      const cashBtn = document.createElement('button');
      cashBtn.type = 'button';
      cashBtn.className = 'assistant-option-btn';
      cashBtn.textContent = f.payment_cash;

      const cardBtn = document.createElement('button');
      cardBtn.type = 'button';
      cardBtn.className = 'assistant-option-btn gold';
      cardBtn.textContent = f.payment_card;
      const cardReady = !!(window.StripeTerminalPay && window.STRIPE_TERMINAL_CONFIG && window.STRIPE_TERMINAL_CONFIG.configured);
      if (!cardReady) cardBtn.disabled = true;

      btnRow.appendChild(cashBtn);
      btnRow.appendChild(cardBtn);
      wrap.appendChild(btnRow);

      const statusEl = document.createElement('div');
      statusEl.className = 'assistant-review-card d-none';
      wrap.appendChild(statusEl);

      function setStatus(text, isError) {
        if (!text) { statusEl.classList.add('d-none'); statusEl.textContent = ''; return; }
        statusEl.classList.remove('d-none');
        statusEl.style.color = isError ? '#a3231d' : 'inherit';
        statusEl.textContent = text;
      }

      body.appendChild(wrap);
      scrollToBottom();

      cashBtn.addEventListener('click', () => {
        wrap.remove();
        resolve({ method: 'cash', stripePaymentIntentId: null });
      });

      cardBtn.addEventListener('click', async () => {
        if (!cardReady) { setStatus(f.card_not_configured, true); return; }
        cashBtn.disabled = true;
        cardBtn.disabled = true;
        const statusLabels = {
          reader_discovering: f.card_reader_discovering,
          reader_connecting: f.card_reader_connecting,
          creating_intent: f.card_creating_intent,
          present_card: f.card_present_card,
          processing: f.card_processing,
        };
        try {
          const paymentIntentId = await StripeTerminalPay.collectPayment(
            amount, { context: 'debt_payment', reference },
            { onStatus: (key) => setStatus(statusLabels[key] || key, false) }
          );
          wrap.remove();
          resolve({ method: 'card', stripePaymentIntentId: paymentIntentId });
        } catch (err) {
          setStatus(`${f.card_error_prefix} ${err.message}`, true);
          cashBtn.disabled = false;
          cardBtn.disabled = false;
        }
      });
    });
  }

  async function apiCall(url, options) {
    try {
      const res = await fetch(url, options);
      let data = null;
      try { data = await res.json(); } catch (e) { /* empty/non-JSON body */ }
      if (!res.ok) {
        return { ok: false, data, message: (data && data.message) || t('generic_error') };
      }
      return { ok: true, data };
    } catch (e) {
      return { ok: false, message: t('connection_error') };
    }
  }

  function fmtMoney(n) {
    return new Intl.NumberFormat(LOCALE[state.lang], {
      style: 'currency', currency: 'EUR', minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(Number(n || 0));
  }

  function backButton() {
    return { label: t('back'), action: showMainMenu };
  }

  function catBack(fn) {
    return { label: t('back'), action: fn };
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  // Structured "please review" card shown before confirming a write action —
  // replaces the old one-line "Please confirm: X?" sentence with a labeled
  // field-by-field summary, so the person can actually check every value
  // (not just the one or two the old sentence happened to mention) before
  // committing. `rows` is [label, value] pairs; falsy values are skipped so
  // optional fields the person left blank don't show up as "Phone: ".
  // `value` may itself contain safe HTML (e.g. already-escaped/bold text) —
  // callers are responsible for escaping any raw user input they pass in.
  function buildReviewCard(title, rows) {
    const rowsHtml = rows
      .filter((r) => r && r[1] !== null && r[1] !== undefined && r[1] !== '')
      .map(([label, value]) => `<div class="assistant-review-row"><span class="assistant-review-label">${escapeHtml(label)}</span><span class="assistant-review-value">${value}</span></div>`)
      .join('');
    return `<div class="assistant-review-card"><div class="assistant-review-title">${escapeHtml(title)}</div>${rowsHtml}</div>`;
  }

  // Generic "type to search, pick from a real list" combobox — the same
  // suggestion-dropdown UX Quick Sell already used for products, factored
  // out so Debts (clients), Salary (sellers), and Purchasing (products) can
  // all use it instead of a blind free-text field. Renders into `listEl`
  // (an `.assistant-suggest-list` sibling of `input`, inside an
  // `.assistant-suggest-wrap`) and closes itself on an outside click within
  // `form`.
  //
  //   fetchItems(query)  → Promise<item[]>   looked up on every keystroke
  //   getLabel(item)     → string             shown as the row's main text
  //                                            and written into the input on pick
  //   getMeta(item)      → string|null        optional right-aligned hint text
  //   onSelect(item)     → called when a suggestion is clicked
  //   onChange()         → called whenever the person types (selection cleared)
  //   strict             → if true, colors the input green once a real item
  //                         is selected and red once it's been typed away
  //                         from a selection — used where only an existing
  //                         record is valid (e.g. paying salary to a real
  //                         seller), not for fields where typing something
  //                         new is fine (e.g. a new client's name).
  //
  // Returns { getSelected } so the submit handler can check whether the
  // current text is bound to a real record or just free-typed.
  function attachSuggestCombobox(form, input, listEl, { fetchItems, getLabel, getMeta, onSelect, onChange, strict, emptyLabel }) {
    let selected = null;

    function hide() {
      listEl.style.display = 'none';
      listEl.innerHTML = '';
    }

    function markValidity() {
      if (!strict) return;
      input.classList.remove('assistant-combobox-valid', 'assistant-combobox-invalid');
      if (!input.value.trim()) return;
      input.classList.add(selected ? 'assistant-combobox-valid' : 'assistant-combobox-invalid');
    }

    function render(items) {
      if (!items.length) {
        listEl.innerHTML = `<div class="assistant-suggest-item" style="cursor:default;color:var(--ink-soft);">${emptyLabel || t('no_matches')}</div>`;
        listEl.style.display = 'block';
        return;
      }
      listEl.innerHTML = '';
      items.slice(0, 8).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'assistant-suggest-item';
        const meta = getMeta ? getMeta(item) : null;
        row.innerHTML = `<span class="name">${escapeHtml(getLabel(item))}</span>${meta ? `<span class="meta">${escapeHtml(meta)}</span>` : ''}`;
        row.addEventListener('click', () => {
          input.value = getLabel(item);
          selected = item;
          hide();
          markValidity();
          if (onSelect) onSelect(item);
        });
        listEl.appendChild(row);
      });
      listEl.style.display = 'block';
    }

    const lookup = debounce(async (query) => {
      const items = await fetchItems(query);
      render(items || []);
    }, 250);

    input.addEventListener('input', () => {
      selected = null;
      markValidity();
      if (onChange) onChange(null);
      lookup(input.value.trim());
    });
    input.addEventListener('focus', () => lookup(input.value.trim()));
    document.addEventListener('click', (e) => {
      if (!form.contains(e.target)) hide();
    });

    return { getSelected: () => selected, hide, markValidity };
  }

  // ---------------- Smart insights (proactive glance shown on greeting) ---
  // Silently fetches the same numbers the "report" menu shows, and — only
  // if there's actually something worth flagging (low stock, open debts,
  // unpaid invoices) — renders a compact, color-coded snapshot before the
  // greeting. Fails silently for sellers (endpoint is admin-only) or if
  // offline, so it never blocks the normal greeting flow.
  async function showSmartInsights() {
    const res = await apiCall('/assistant/api/summary');
    if (!res.ok || !res.data) return;
    const d = res.data;
    const r = I18N[state.lang].report;
    const hasAlert = (d.debts_open_count > 0) || (d.low_stock_count > 0) || (Number(d.factures_unpaid_total) > 0);
    const chip = (label, value, warn) =>
      `<div class="assistant-insight-chip${warn ? ' warn' : ''}"><span class="ai-label">${escapeHtml(label)}</span><span class="ai-value">${escapeHtml(value)}</span></div>`;
    const html = `
      <div class="assistant-insight-heading">${r.title}</div>
      <div class="assistant-insight-grid">
        ${chip(r.sales_today, fmtMoney(d.sales_today))}
        ${chip(r.profit_today, fmtMoney(d.profit_today))}
        ${chip(r.kasse_balance, fmtMoney(d.kasse_balance))}
        ${chip(r.debts_open, `${fmtMoney(d.debts_open_total)} (${d.debts_open_count})`, d.debts_open_count > 0)}
        ${chip(r.factures_unpaid, fmtMoney(d.factures_unpaid_total), Number(d.factures_unpaid_total) > 0)}
        ${chip(r.low_stock, `${d.low_stock_count} ${r.items}`, d.low_stock_count > 0)}
      </div>`;
    addBotMessage(html, { speak: false });

    if (hasAlert) {
      const quick = [];
      if (d.low_stock_count > 0) quick.push({ label: '📦 ' + r.low_stock, action: showStockMenu });
      if (d.debts_open_count > 0) quick.push({ label: '💸 ' + r.debts_open, action: showDebtsMenu });
      if (Number(d.factures_unpaid_total) > 0) quick.push({ label: '🧾 ' + r.factures_unpaid, action: showFacturesMenu });
      if (quick.length) addOptions(quick);
    }
  }

  // ---------------- Main menu ----------------
  function showMainMenu(isGreeting) {
    state.currentCategory = 'main';
    updateBackButtonVisibility();

    if (isGreeting) showSmartInsights();
    addBotMessage(isGreeting ? t('greeting') : t('ask_next'));

    // Always provide an explicit back option so the user can return
    // if they change their mind (covers cases where the list-specific
    // back option is not rendered).
    addOptions([
      { label: t('menu.report'), action: showReportMenu },
      { label: t('menu.sell'), action: showSellMenu },
      // Debts and Factures open for a seller once the matching category is
      // granted (see PERMISSION_CATEGORY_TOOLS in app.py). Sellers/Salary
      // stay admin-only always — managing other seller accounts or pay
      // isn't one of the grantable "KI-Assistent" checkboxes on purpose.
      (ROLE === 'admin' || GRANTED_CATEGORIES.has('debts')) ? { label: t('menu.debts'), action: showDebtsMenu } : null,
      (ROLE === 'admin' || GRANTED_CATEGORIES.has('factures')) ? { label: t('menu.factures'), action: showFacturesMenu } : null,
      { label: t('menu.items'), action: showItemsMenu },
      { label: t('menu.orders'), action: showOrdersMenu },
      { label: t('menu.stock'), action: showStockMenu },
      { label: t('menu.sales'), action: showSalesMenu },
      ROLE === 'admin' ? { label: t('menu.sellers'), action: showSellersMenu } : null,
      ROLE === 'admin' ? { label: t('menu.salary'), action: showSalaryMenu } : null,
      CAN_KASSE ? { label: t('menu.kasse'), action: showKasseMenu } : null,
      backButton(),
    ].filter(Boolean), { tiles: true });
  }


  // ---------------- Reports ----------------
  function showReportMenu() {
    state.currentCategory = 'report';
    updateBackButtonVisibility();
    addBotMessage(t('report.which'));
    addOptions([{ label: t('report.overview'), action: runSummaryReport }, backButton()]);
  }

  async function runSummaryReport() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/summary');
    typing.remove();
    if (!res.ok) { addBotMessage(res.message); addOptions([catBack(showReportMenu)]); return; }
    const d = res.data;
    const r = I18N[state.lang].report;
    addBotMessage(`
      <strong>${r.title}</strong><br>
      ${r.sales_today}: <strong>${fmtMoney(d.sales_today)}</strong><br>
      ${r.profit_today}: <strong>${fmtMoney(d.profit_today)}</strong><br>
      ${r.sales_month}: <strong>${fmtMoney(d.sales_month)}</strong><br>
      ${r.kasse_balance}: <strong>${fmtMoney(d.kasse_balance)}</strong><br>
      ${r.debts_open}: <strong>${fmtMoney(d.debts_open_total)}</strong> (${d.debts_open_count})<br>
      ${r.factures_unpaid}: <strong>${fmtMoney(d.factures_unpaid_total)}</strong><br>
      ${r.low_stock}: <strong>${d.low_stock_count}</strong> ${r.items}
    `);
    addOptions([catBack(showReportMenu)]);
  }

  // ---------------- Debts ----------------
  function showDebtsMenu() {
    state.currentCategory = 'debts';
    updateBackButtonVisibility();
    addBotMessage(t('debts.menu_title'));
    addOptions([
      { label: t('debts.view'), action: runOpenDebts },
      { label: t('debts.view_clients'), action: runClients },
      { label: t('debts.add'), action: showAddDebtForm },
      { label: t('debts.delete_all'), action: runDeleteAllDebts },
      backButton(),
    ]);
  }

  async function runDeleteAllDebts() {
    const confirmed = await confirmAction(t('debts.confirm_delete_all'));
    if (!confirmed) { addBotMessage(t('confirm_cancelled')); addOptions([catBack(showDebtsMenu)]); return; }
    const typing = showTyping();
    const res = await apiCall('/assistant/api/debts/delete_all', { method: 'POST' });
    typing.remove();
    if (res.ok && res.data.success) {
      addBotMessage(t('debts.deleted_all')(res.data.deleted_count));
    } else {
      addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
    }
    addOptions([catBack(showDebtsMenu)]);
  }

  async function fetchOpenDebts() {
    const res = await apiCall('/assistant/api/debts/open');
    if (!res.ok) return { debts: null, message: (res.data && res.data.message) || res.message || t('generic_error') };
    return { debts: res.data.debts, message: null };
  }

  async function runOpenDebts() {
    const typing = showTyping();
    const { debts, message } = await fetchOpenDebts();
    typing.remove();
    if (debts === null) { addBotMessage(message || t('generic_error')); addOptions([catBack(showDebtsMenu)]); return; }
    if (!debts.length) { addBotMessage(t('debts.none')); addOptions([catBack(showDebtsMenu)]); return; }
    addBotMessage(t('debts.count')(debts.length));
    debts.forEach((debt) => renderDebtRow(debt));
    addOptions([catBack(showDebtsMenu)]);
  }

  function renderDebtRow(debt) {
    const msgDiv = addBotMessage(
      `<strong>${escapeHtml(debt.client_name)}</strong> — ${fmtMoney(debt.amount)}` +
      (debt.reference_number ? `<br><span style="color:var(--ink-soft);font-size:0.78em;">${t('debts.reference_label')} ${escapeHtml(debt.reference_number)}</span>` : '') +
      (debt.phone_number ? `<br><span style="color:var(--ink-soft);font-size:0.8em;">${escapeHtml(debt.phone_number)}</span>` : ''),
      { speak: false }
    );
    const btn = document.createElement('button');
    btn.className = 'assistant-option-btn gold';
    btn.style.marginTop = '0.4rem';
    btn.textContent = t('debts.mark_paid');
    btn.addEventListener('click', async () => {
      const confirmed = await confirmAction(
        `${t('confirm_prompt')} <strong>${escapeHtml(debt.client_name)}</strong> — ${fmtMoney(debt.amount)}: ${t('debts.mark_paid')}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
      btn.disabled = true;
      const payment = await collectPaymentMethod(debt.amount, debt.debt_id, 'debts');
      btn.textContent = '…';
      const payRes = await apiCall(`/assistant/api/debts/${encodeURIComponent(debt.debt_id)}/pay`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payment_method: payment.method, stripe_payment_intent_id: payment.stripePaymentIntentId }),
      });
      if (payRes.ok) {
        msgDiv.style.opacity = '0.5';
        btn.textContent = t('debts.marked');
        await runOpenDebts();
      } else {
        btn.disabled = false;
        btn.textContent = t('debts.mark_paid');
        addBotMessage((payRes.data && payRes.data.message) || payRes.message || t('generic_error'));
      }
    });

    const editBtn = document.createElement('button');
    editBtn.className = 'assistant-option-btn';
    editBtn.style.marginTop = '0.4rem';
    editBtn.textContent = t('debts.edit_btn');
    editBtn.addEventListener('click', () => showEditDebtForm(debt));

    msgDiv.appendChild(document.createElement('br'));
    msgDiv.appendChild(btn);
    msgDiv.appendChild(document.createTextNode(' '));
    msgDiv.appendChild(editBtn);
  }

  // Corrects a debt's own details (client name, remaining amount, phone,
  // description) — distinct from recording an actual payment against it.
  function showEditDebtForm(debt) {
    const f = I18N[state.lang].debts.form;
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      ${debt.reference_number ? `<div class="assistant-inline-hint">${f.reference}: <strong>${escapeHtml(debt.reference_number)}</strong></div>` : ''}
      <div>
        <label>${f.client}</label>
        <input type="text" data-field="client_name" placeholder="${f.client_ph}" value="${escapeHtml(debt.client_name || '')}">
      </div>
      <div class="row2">
        <div>
          <label>${f.amount}</label>
          <input type="number" step="0.01" min="0" data-field="amount" placeholder="0.00" value="${debt.amount != null ? debt.amount : ''}">
        </div>
        <div>
          <label>${f.phone}</label>
          <input type="text" data-field="phone_number" placeholder="${f.phone_ph}" value="${escapeHtml(debt.phone_number || '')}">
        </div>
      </div>
      <div>
        <label>${f.description}</label>
        <input type="text" data-field="description" placeholder="${f.description_ph}" value="${escapeHtml(debt.description || '')}">
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_edit_debt">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_edit_debt').addEventListener('click', () => {
      form.remove();
    });

    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} <strong>${escapeHtml(payload.client_name)}</strong> — ${fmtMoney(payload.amount)}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall(`/assistant/api/debts/${encodeURIComponent(debt.debt_id)}/edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('debts.edited')(escapeHtml(payload.client_name)));
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('debts.view'), action: runOpenDebts }, catBack(showDebtsMenu)]);
    });
  }

  function showAddDebtForm(prefill) {
    const f = I18N[state.lang].debts.form;
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div class="assistant-suggest-wrap">
        <label>${f.client}</label>
        <input type="text" data-field="client_name" autocomplete="off" placeholder="${f.client_ph}" value="${escapeHtml(prefill && prefill.name || '')}">
        <div class="assistant-suggest-list" style="display:none;"></div>
      </div>
      <div class="row2">
        <div>
          <label>${f.amount}</label>
          <input type="number" step="0.01" min="0" data-field="amount" placeholder="0.00" value="${prefill && prefill.amount != null ? prefill.amount : ''}">
        </div>
        <div>
          <label>${f.phone}</label>
          <input type="text" data-field="phone_number" placeholder="${f.phone_ph}">
        </div>
      </div>
      <div>
        <label>${f.description}</label>
        <input type="text" data-field="description" placeholder="${f.description_ph}">
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_debts">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    // Type-to-search against real clients instead of a blind free-text
    // field — picking an existing client avoids accidentally creating a
    // near-duplicate ("Ahmed" vs "Ahmad"). Typing a name that doesn't
    // match anyone is still fine (not `strict`): a brand-new client is a
    // completely normal case here.
    let allClients = null;
    attachSuggestCombobox(
      form,
      form.querySelector('[data-field="client_name"]'),
      form.querySelector('.assistant-suggest-list'),
      {
        fetchItems: async (query) => {
          if (!allClients) {
            const res = await apiCall('/assistant/api/clients');
            allClients = (res.ok && res.data.clients) || [];
          }
          const q = query.toLowerCase();
          return q ? allClients.filter((c) => c.client_name.toLowerCase().includes(q)) : allClients;
        },
        getLabel: (c) => c.client_name,
        getMeta: (c) => (c.has_unpaid ? fmtMoney(c.total_unpaid) : null),
        strict: false,
      }
    );

    form.querySelector('#assistant_inline_back_debts').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showDebtsMenu)]);
    });

    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} ${t('debts.add')} — <strong>${escapeHtml(payload.client_name)}</strong>, ${fmtMoney(payload.amount)}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/debts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('debts.added')(escapeHtml(payload.client_name), fmtMoney(payload.amount)));
        if (res.data.reference_number) {
          addBotMessage(`${I18N[state.lang].debts.form.reference}: <strong>${escapeHtml(res.data.reference_number)}</strong>`, { speak: false });
        }
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('debts.add_more'), action: showAddDebtForm }, catBack(showDebtsMenu)]);
    });
  }

  // ---------------- Clients (the people who owe debts) ----------------
  // Kept deliberately separate from the debts list above: a "debt" is one
  // amount owed, a "client" is the person — one client can have several
  // debts (paid and/or open), so they need their own view/lookup.
  async function runClients() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/clients');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showDebtsMenu)]); return; }
    const clients = res.data.clients || [];
    if (!clients.length) { addBotMessage(t('debts.clients.none')); addOptions([catBack(showDebtsMenu)]); return; }
    addBotMessage(t('debts.clients.list_title')(clients.length));
    clients.forEach((client) => renderClientRow(client));
    addOptions([catBack(showDebtsMenu)]);
  }

  function renderClientRow(client) {
    const cl = I18N[state.lang].debts.clients;
    const statusLabel = client.has_unpaid ? cl.open_label : cl.paid_label;
    const msgDiv = addBotMessage(
      `<strong>${escapeHtml(client.client_name)}</strong> — ${statusLabel}` +
      (client.has_unpaid ? ` (${fmtMoney(client.total_unpaid)})` : ''),
      { speak: false }
    );
    const btn = document.createElement('button');
    btn.className = 'assistant-option-btn';
    btn.style.marginTop = '0.4rem';
    btn.textContent = cl.history_title(client.client_name).replace(/<[^>]+>/g, '');
    btn.addEventListener('click', () => showClientDebts(client.client_name));
    msgDiv.appendChild(document.createElement('br'));
    msgDiv.appendChild(btn);
  }

  async function showClientDebts(clientName) {
    const cl = I18N[state.lang].debts.clients;
    const typing = showTyping();
    const res = await apiCall(`/assistant/api/clients/${encodeURIComponent(clientName)}/debts`);
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([{ label: cl.back_to_clients, action: runClients }, catBack(showDebtsMenu)]); return; }
    const debts = res.data.debts || [];
    addBotMessage(cl.history_title(clientName));
    if (!debts.length) {
      addBotMessage(cl.no_debts);
    } else {
      debts.forEach((debt) => renderClientDebtRow(debt, cl));
    }
    addOptions([{ label: cl.back_to_clients, action: runClients }, catBack(showDebtsMenu)]);
  }

  function renderClientDebtRow(debt, cl) {
    const statusLabel = debt.paid ? cl.paid_label : cl.open_label;
    const msgDiv = addBotMessage(
      `${escapeHtml(debt.description || '—')} — ${fmtMoney(debt.amount)} <em>(${statusLabel})</em>` +
      (debt.created_at ? `<br><span style="color:var(--ink-soft);font-size:0.8em;">${escapeHtml(debt.created_at)}</span>` : ''),
      { speak: false }
    );
    if (!debt.paid) {
      const btn = document.createElement('button');
      btn.className = 'assistant-option-btn gold';
      btn.style.marginTop = '0.4rem';
      btn.textContent = t('debts.mark_paid');
      btn.addEventListener('click', async () => {
        const confirmed = await confirmAction(
          `${t('confirm_prompt')} ${escapeHtml(debt.description || '')} — ${fmtMoney(debt.amount)}: ${t('debts.mark_paid')}?`
        );
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        btn.disabled = true;
        const payment = await collectPaymentMethod(debt.amount, debt.debt_id, 'debts');
        btn.textContent = '…';
        const payRes = await apiCall(`/assistant/api/debts/${encodeURIComponent(debt.debt_id)}/pay`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ payment_method: payment.method, stripe_payment_intent_id: payment.stripePaymentIntentId }),
        });
        if (payRes.ok) {
          msgDiv.style.opacity = '0.5';
          btn.textContent = t('debts.marked');
          await showClientDebts(debt.client_name);
        } else {
          btn.disabled = false;
          btn.textContent = t('debts.mark_paid');
          addBotMessage((payRes.data && payRes.data.message) || payRes.message || t('generic_error'));
        }
      });
      msgDiv.appendChild(document.createElement('br'));
      msgDiv.appendChild(btn);
    }
  }

  // ---------------- Factures ----------------
  function showFacturesMenu() {
    state.currentCategory = 'factures';
    updateBackButtonVisibility();
    addBotMessage(t('factures.menu_title'));
    addOptions([
      { label: t('factures.view'), action: runOpenFactures },
      { label: t('factures.add'), action: () => showAddFactureForm() },
      { label: t('scan.button'), action: startFacturePhotoScan },
      backButton(),
    ]);
  }

  // Hidden file input reused for the "photograph invoice" feature.
  // capture="environment" opens the rear camera directly on phones; PDFs
  // are picked via the regular file browser (email/exported invoices are
  // often PDFs, not photos, and their text layer gives exact — not OCR-
  // guessed — results, so it's worth supporting both).
  let scanFileInput = null;
  function startFacturePhotoScan() {
    if (!scanFileInput) {
      scanFileInput = document.createElement('input');
      scanFileInput.type = 'file';
      scanFileInput.accept = 'image/*,application/pdf';
      scanFileInput.capture = 'environment';
      scanFileInput.style.display = 'none';
      document.body.appendChild(scanFileInput);
      scanFileInput.addEventListener('change', onFacturePhotoSelected);
    }
    scanFileInput.value = '';
    scanFileInput.click();
  }

  async function onFacturePhotoSelected(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    addUserMessage(t('scan.button'));
    const typing = showTyping();
    addBotMessage(t('scan.uploading'), { speak: false });

    const formData = new FormData();
    formData.append('image', file);
    let res;
    try {
      const raw = await fetch('/factures/ocr', { method: 'POST', body: formData });
      const data = await raw.json().catch(() => ({}));
      res = { ok: raw.ok, data };
    } catch (err) {
      res = { ok: false, data: null };
    }
    typing.remove();

    if (!res.ok || !res.data || !res.data.ok) {
      addBotMessage((res.data && res.data.message) || t('scan.failed'));
      showAddFactureForm();
      return;
    }
    addBotMessage(t('scan.found'));
    // The amount is either keyword-anchored (found right next to a
    // "Total"/"Gesamtbetrag"/"Amount Due" label — high confidence) or a
    // fallback guess (just the largest number on the page). Only nudge the
    // person to double-check in the fallback case, so precise reads don't
    // get an unnecessary warning.
    if (res.data.amount_source === 'fallback') {
      addBotMessage(t('scan.check_amount'), { speak: false });
    }
    showAddFactureForm({
      issuer: res.data.issuer,
      amount: res.data.amount,
      facture_type: res.data.facture_type,
      issue_date: res.data.issue_date,
      due_date: res.data.due_date,
    });
  }

  // Fetches a page of `limit` unpaid invoices starting at `offset`, optionally
  // filtered by issuer via `q`. Pages instead of pulling the whole unpaid set
  // in one go — with a large factures table (thousands, potentially millions
  // of rows) that's the only way this stays fast and the chat log stays
  // readable. offset === 0 is treated as "fresh list": it (re)shows the
  // search box and the total count; subsequent pages just append more cards.
  const FACTURES_PAGE_SIZE = 5;

  async function runOpenFactures(offset = 0, q = '') {
    const typing = showTyping();
    const params = new URLSearchParams({ limit: String(FACTURES_PAGE_SIZE), offset: String(offset) });
    if (q) params.set('q', q);
    const res = await apiCall(`/assistant/api/factures/unpaid?${params.toString()}`);
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showFacturesMenu)]); return; }
    const { factures, total, has_more } = res.data;

    if (offset === 0) {
      const searchWrap = document.createElement('div');
      searchWrap.className = 'assistant-inline-form';
      searchWrap.innerHTML = `<input type="text" placeholder="${t('factures.search_ph')}" value="${escapeHtml(q)}">`;
      body.appendChild(searchWrap);
      scrollToBottom();
      const input = searchWrap.querySelector('input');
      input.focus();
      let debounceTimer;
      // Debounced so a search over a huge invoice table doesn't fire a
      // request per keystroke — only once typing pauses.
      input.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
          searchWrap.remove();
          runOpenFactures(0, input.value.trim());
        }, 350);
      });

      if (!total) { addBotMessage(t('factures.none')); addOptions([catBack(showFacturesMenu)]); return; }
      addBotMessage(`${t('factures.count')(total)}`, { speak: false });
    }

    const typeLabels = I18N[state.lang].factures.types;
    factures.forEach((f) => {
      const typeLabel = f.facture_type_label || typeLabels[f.facture_type] || f.facture_type;
      const msgDiv = addBotMessage(
        `<strong>${escapeHtml(f.issuer)}</strong> (${escapeHtml(typeLabel)}) — ${fmtMoney(f.amount)}` +
        (f.due_date_display ? `<br><span style="color:var(--ink-soft);font-size:0.8em;">${t('factures.due')}: ${f.due_date_display}</span>` : ''),
        { speak: false }
      );

      const payBtn = document.createElement('button');
      payBtn.className = 'assistant-option-btn gold';
      payBtn.style.marginTop = '0.4rem';
      payBtn.textContent = t('factures.mark_paid');

      const editBtn = document.createElement('button');
      editBtn.className = 'assistant-option-btn';
      editBtn.style.marginTop = '0.4rem';
      editBtn.style.marginLeft = '0.4rem';
      editBtn.textContent = t('factures.edit_btn');

      const delBtn = document.createElement('button');
      delBtn.className = 'assistant-option-btn danger';
      delBtn.style.marginTop = '0.4rem';
      delBtn.style.marginLeft = '0.4rem';
      delBtn.textContent = t('factures.delete_btn');

      payBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(
          `${t('confirm_prompt')} <strong>${escapeHtml(f.issuer)}</strong> — ${fmtMoney(f.amount)}: ${t('factures.mark_paid')}?`
        );
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        payBtn.disabled = true;
        payBtn.textContent = '…';
        const payRes = await apiCall(`/assistant/api/factures/${f.id}/pay`, { method: 'POST' });
        if (payRes.ok) {
          msgDiv.style.opacity = '0.5';
          payBtn.textContent = t('factures.marked');
          editBtn.disabled = true;
          delBtn.disabled = true;
        } else {
          payBtn.disabled = false;
          payBtn.textContent = t('factures.mark_paid');
          addBotMessage((payRes.data && payRes.data.message) || payRes.message || t('generic_error'));
        }
      });

      editBtn.addEventListener('click', () => {
        showAddFactureForm({
          facture_type: f.facture_type, issuer: f.issuer, amount: f.amount, due_date: f.due_date,
        }, f.id);
      });

      delBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(t('factures.confirm_delete')(escapeHtml(f.issuer)));
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        payBtn.disabled = true;
        editBtn.disabled = true;
        delBtn.disabled = true;
        delBtn.textContent = '…';
        const delRes = await apiCall(`/assistant/api/factures/${f.id}`, { method: 'DELETE' });
        if (delRes.ok && delRes.data.success) {
          msgDiv.style.opacity = '0.35';
          msgDiv.style.textDecoration = 'line-through';
          delBtn.textContent = t('factures.deleted');
        } else {
          payBtn.disabled = false;
          editBtn.disabled = false;
          delBtn.disabled = false;
          delBtn.textContent = t('factures.delete_btn');
          addBotMessage((delRes.data && delRes.data.message) || delRes.message || t('generic_error'));
        }
      });

      msgDiv.appendChild(document.createElement('br'));
      msgDiv.appendChild(payBtn);
      msgDiv.appendChild(editBtn);
      msgDiv.appendChild(delBtn);
    });

    if (has_more) {
      const remaining = total - (offset + factures.length);
      addOptions([
        { label: t('factures.load_more')(remaining), action: () => runOpenFactures(offset + factures.length, q) },
        catBack(showFacturesMenu),
      ]);
    } else {
      addOptions([catBack(showFacturesMenu)]);
    }
  }

  // `editId` turns this into an edit form: PUT to the existing invoice
  // instead of POSTing a new one, with a confirm prompt and confirmation
  // message worded for an update rather than a fresh add.
  function showAddFactureForm(prefill, editId) {
    const isEdit = editId != null;
    const f = I18N[state.lang].factures.form;
    const types = I18N[state.lang].factures.types;
    const p = prefill || {};
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    if (isEdit) {
      const title = document.createElement('div');
      title.style.fontWeight = '600';
      title.style.marginBottom = '0.4rem';
      title.textContent = f.edit_title;
      form.appendChild(title);
    }
    form.innerHTML += `
      <div>
        <label>${f.type}</label>
        <select data-field="facture_type">
          ${Object.keys(types).map((k) => `<option value="${k}"${k === (p.facture_type || 'other') ? ' selected' : ''}>${types[k]}</option>`).join('')}
        </select>
      </div>
      <div>
        <label>${f.issuer}</label>
        <input type="text" data-field="issuer" placeholder="${f.issuer_ph}" value="${escapeHtml(p.issuer || '')}">
      </div>
      <div class="row2">
        <div>
          <label>${f.amount}</label>
          <input type="number" step="0.01" min="0" data-field="amount" placeholder="0.00" value="${p.amount != null ? p.amount : ''}">
        </div>
        <div>
          <label>${f.due_date}</label>
          <input type="date" data-field="due_date" value="${p.due_date || ''}">
        </div>
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_factures">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_factures').addEventListener('click', () => {
      form.remove();
      addOptions(isEdit ? [{ label: t('factures.view'), action: () => runOpenFactures() }, catBack(showFacturesMenu)] : [catBack(showFacturesMenu)]);
    });

    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} ${isEdit ? f.edit_title : t('factures.add')} — <strong>${escapeHtml(payload.issuer)}</strong>, ${fmtMoney(payload.amount)}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = isEdit ? f.saving_edit : f.saving;

      const res = await apiCall(isEdit ? `/assistant/api/factures/${editId}` : '/assistant/api/factures', {
        method: isEdit ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(isEdit
          ? t('factures.edited')(escapeHtml(payload.issuer))
          : `✅ ${escapeHtml(payload.issuer)} — ${fmtMoney(payload.amount)}`);
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions(isEdit
        ? [{ label: t('factures.view'), action: () => runOpenFactures() }, catBack(showFacturesMenu)]
        : [{ label: t('factures.add_more'), action: () => showAddFactureForm() }, catBack(showFacturesMenu)]);
    });
  }

  // ---------------- Stock ----------------
  function showStockMenu() {
    state.currentCategory = 'stock';
    updateBackButtonVisibility();
    addBotMessage(t('stock.menu_title'));
    addOptions([{ label: t('stock.view'), action: runLowStock }, backButton()]);
  }

  async function runLowStock() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/stock/low');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showStockMenu)]); return; }
    const items = res.data.items;
    if (!items.length) {
      addBotMessage(t('stock.none'));
    } else {
      addBotMessage(t('stock.warning') + '<br>' + items.map((i) => '• ' + escapeHtml(i.message)).join('<br>'));
    }
    addOptions([catBack(showStockMenu)]);
  }

  // ---------------- Kasse ----------------
  function showKasseMenu() {
    state.currentCategory = 'kasse';
    updateBackButtonVisibility();
    addBotMessage(t('kasse.menu_title'));
    addOptions([
      { label: t('kasse.view'), action: runKasseToday },
      { label: t('kasse.history'), action: runKasseHistory },
      { label: t('kasse.add'), action: showAddKasseForm },
      backButton(),
    ]);
  }

  async function runKasseToday() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/kasse/today');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showKasseMenu)]); return; }
    const d = res.data;
    const k = I18N[state.lang].kasse;
    addBotMessage(`
      <strong>${k.title}</strong><br>
      ${k.balance}: <strong>${fmtMoney(d.balance)}</strong><br>
      ${k.sales_today}: ${fmtMoney(d.sales_today)}<br>
      ${k.purchases_today}: ${fmtMoney(d.purchases_today)}${d.cash_deposits_today ? `<br>${k.deposits_today}: ${fmtMoney(d.cash_deposits_today)}` : ''}${d.cash_withdrawals_today ? `<br>${k.withdrawals_today}: ${fmtMoney(d.cash_withdrawals_today)}` : ''}
    `);
    addOptions([catBack(showKasseMenu)]);
  }

  async function runKasseHistory() {
    const k = I18N[state.lang].kasse;
    const typing = showTyping();
    const res = await apiCall('/assistant/api/kasse/transactions');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showKasseMenu)]); return; }
    const txs = res.data.transactions;
    if (!txs.length) { addBotMessage(k.history_none); addOptions([catBack(showKasseMenu)]); return; }
    addBotMessage(k.history_title(txs.length));
    addBotMessage(txs.map((tx) => {
      const types = I18N[state.lang].kasse.types;
      const label = types[tx.type] || tx.type;
      const sign = tx.type === 'auszahlung' ? '-' : '+';
      return `• ${tx.date || ''} — ${label} ${sign}${fmtMoney(tx.amount)}${tx.description ? ' — ' + escapeHtml(tx.description) : ''}${tx.username ? ' (' + escapeHtml(tx.username) + ')' : ''}`;
    }).join('<br>'), { speak: false });
    addOptions([catBack(showKasseMenu)]);
  }

  function showAddKasseForm() {
    const f = I18N[state.lang].kasse.form;
    const types = I18N[state.lang].kasse.types;
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div>
        <label>${f.type}</label>
        <select data-field="type">
          <option value="einzahlung">${types.einzahlung}</option>
          <option value="auszahlung">${types.auszahlung}</option>
        </select>
      </div>
      <div class="row2">
        <div>
          <label>${f.amount}</label>
          <input type="number" step="0.01" min="0" data-field="amount" placeholder="0.00">
        </div>
        <div>
          <label>${f.description}</label>
          <input type="text" data-field="description" placeholder="${f.description_ph}">
        </div>
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_kasse">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_kasse').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showKasseMenu)]);
    });


    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} ${types[payload.type] || payload.type} — ${fmtMoney(payload.amount)}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/kasse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('kasse.booked')(types[payload.type], fmtMoney(payload.amount)));
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([catBack(showKasseMenu)]);
    });
  }

  // ---------------- Items ----------------
  function showItemsMenu() {
    state.currentCategory = 'items';
    updateBackButtonVisibility();
    addBotMessage(t('items.menu_title'));
    addOptions([
      { label: t('items.view'), action: runListItems },
      { label: t('items.low_stock'), action: runLowStock },
      canUseTool('add_item') ? { label: t('items.add'), action: () => showAddItemForm() } : null,
      backButton(),
    ].filter(Boolean));
  }

  async function runListItems() {
    const wrap = document.createElement('div');
    wrap.className = 'assistant-msg bot';
    wrap.innerHTML = `
      <div class="assistant-suggest-wrap" style="margin-bottom:0.5rem;">
        <input type="text" data-role="items-search" autocomplete="off" placeholder="${t('items.search_ph')}">
      </div>
      <div data-role="items-results"></div>
    `;
    body.appendChild(wrap);
    scrollToBottom();
    const resultsEl = wrap.querySelector('[data-role="items-results"]');
    const searchInput = wrap.querySelector('[data-role="items-search"]');

    function renderRow(i) {
      const row = document.createElement('div');
      row.style.marginBottom = '0.6rem';
      row.innerHTML = `<strong>${escapeHtml(i.product_name)}</strong> — ${i.quantity}× — ${fmtMoney(i.selling_price)}`;

      // Edit opens the existing (server-rendered) item edit page in a new
      // tab — same route/controller/validation the web app's Inventory
      // page uses, so nothing here duplicates business logic.
      const editBtn = document.createElement('button');
      editBtn.className = 'assistant-option-btn';
      editBtn.style.marginTop = '0.4rem';
      editBtn.textContent = t('items.edit_btn')(i.product_name);
      editBtn.addEventListener('click', () => window.open(`/edit_item/${encodeURIComponent(i.id)}`, '_blank'));

      const delBtn = document.createElement('button');
      delBtn.className = 'assistant-option-btn danger';
      delBtn.style.marginTop = '0.4rem';
      delBtn.style.marginLeft = '0.4rem';
      delBtn.textContent = t('items.delete_btn');
      delBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(t('items.confirm_delete')(escapeHtml(i.product_name)));
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        editBtn.disabled = true;
        delBtn.disabled = true;
        delBtn.textContent = '…';
        const delRes = await apiCall(`/assistant/api/items/${encodeURIComponent(i.id)}`, { method: 'DELETE' });
        if (delRes.ok && delRes.data.success) {
          row.style.opacity = '0.35';
          row.style.textDecoration = 'line-through';
          delBtn.textContent = t('items.deleted');
        } else {
          editBtn.disabled = false;
          delBtn.disabled = false;
          delBtn.textContent = t('items.delete_btn');
          addBotMessage((delRes.data && delRes.data.message) || delRes.message || t('generic_error'));
        }
      });

      row.appendChild(document.createElement('br'));
      row.appendChild(editBtn);
      row.appendChild(delBtn);
      resultsEl.appendChild(row);
    }

    async function renderResults(query) {
      resultsEl.innerHTML = `<span class="assistant-status-hint">${t('loading') || '…'}</span>`;
      const res = await apiCall(`/assistant/api/items${query ? '?q=' + encodeURIComponent(query) : ''}`);
      resultsEl.innerHTML = '';
      if (!res.ok) { resultsEl.textContent = (res.data && res.data.message) || res.message || t('generic_error'); return; }
      const items = res.data.items;
      if (!items.length) { resultsEl.textContent = t('items.none'); return; }
      const title = document.createElement('div');
      title.style.marginBottom = '0.5rem';
      title.innerHTML = t('items.list_title')(items.length);
      resultsEl.appendChild(title);
      items.forEach(renderRow);
      scrollToBottom();
    }

    searchInput.addEventListener('input', debounce(() => renderResults(searchInput.value.trim()), 300));
    await renderResults('');
    addOptions([catBack(showItemsMenu)]);
  }

  function showAddItemForm(prefill) {
    const f = I18N[state.lang].items.form;
    const p = prefill || {};
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div>
        <label>${f.name}</label>
        <input type="text" data-field="product_name" placeholder="${f.name_ph}" value="${escapeHtml(p.product_name || '')}">
      </div>
      <div class="row2">
        <div>
          <label>${f.quantity}</label>
          <input type="number" step="1" min="0" data-field="quantity" placeholder="0" value="${p.quantity != null ? p.quantity : ''}">
        </div>
        <div>
          <label>${f.barcode}</label>
          <input type="text" data-field="barcode" placeholder="${f.barcode_ph}">
        </div>
      </div>
      <div class="row2">
        <div>
          <label>${f.purchase_price}</label>
          <input type="number" step="0.01" min="0" data-field="purchase_price" placeholder="0.00">
        </div>
        <div>
          <label>${f.selling_price}</label>
          <input type="number" step="0.01" min="0" data-field="selling_price" placeholder="0.00">
        </div>
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_items">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_items').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showItemsMenu)]);
    });


    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} ${t('items.add')} — <strong>${escapeHtml(payload.product_name)}</strong>?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('items.added')(escapeHtml(payload.product_name), res.data.barcode));
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('items.add_more'), action: () => showAddItemForm() }, catBack(showItemsMenu)]);
    });
  }

  // ---------------- Sellers ----------------
  function showSellersMenu() {
    state.currentCategory = 'sellers';
    updateBackButtonVisibility();
    addBotMessage(t('sellers.menu_title'));
    addOptions([
      { label: t('sellers.view'), action: runListSellers },
      { label: t('sellers.add'), action: () => showAddSellerForm() },
      backButton(),
    ]);
  }

  async function runListSellers() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/sellers');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showSellersMenu)]); return; }
    const sellers = res.data.sellers;
    const s = I18N[state.lang].sellers;
    if (!sellers.length) { addBotMessage(s.none); addOptions([catBack(showSellersMenu)]); return; }
    addBotMessage(s.list_title(sellers.length));

    sellers.forEach((sel) => {
      const msgDiv = addBotMessage(
        `<strong>${escapeHtml(sel.username)}</strong> — ${fmtMoney(sel.salary)} (${sel.activated ? s.active : s.inactive})`,
        { speak: false }
      );

      // Edit opens the existing admin "edit seller" page — same route,
      // validation and permission checks as the web UI's Sellers page.
      const editBtn = document.createElement('button');
      editBtn.className = 'assistant-option-btn';
      editBtn.style.marginTop = '0.4rem';
      editBtn.textContent = t('sellers.edit_btn')(sel.username);
      editBtn.addEventListener('click', () => window.open(`/admin/sellers/edit/${encodeURIComponent(sel.username)}`, '_blank'));

      const delBtn = document.createElement('button');
      delBtn.className = 'assistant-option-btn danger';
      delBtn.style.marginTop = '0.4rem';
      delBtn.style.marginLeft = '0.4rem';
      delBtn.textContent = t('sellers.delete_btn');
      delBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(t('sellers.confirm_delete')(escapeHtml(sel.username)));
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        editBtn.disabled = true;
        delBtn.disabled = true;
        delBtn.textContent = '…';
        const delRes = await apiCall(`/assistant/api/sellers/${encodeURIComponent(sel.username)}`, { method: 'DELETE' });
        if (delRes.ok && delRes.data.success) {
          msgDiv.style.opacity = '0.35';
          msgDiv.style.textDecoration = 'line-through';
          delBtn.textContent = t('sellers.deleted');
        } else {
          editBtn.disabled = false;
          delBtn.disabled = false;
          delBtn.textContent = t('sellers.delete_btn');
          addBotMessage((delRes.data && delRes.data.message) || delRes.message || t('generic_error'));
        }
      });

      msgDiv.appendChild(document.createElement('br'));
      msgDiv.appendChild(editBtn);
      msgDiv.appendChild(delBtn);
    });

    addOptions([catBack(showSellersMenu)]);
  }

  function showAddSellerForm() {
    const f = I18N[state.lang].sellers.form;
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div>
        <label>${f.username}</label>
        <input type="text" data-field="username" placeholder="${f.username}">
      </div>
      <div class="row2">
        <div>
          <label>${f.password}</label>
          <input type="password" data-field="password" placeholder="••••••">
        </div>
        <div>
          <label>${f.salary}</label>
          <input type="number" step="0.01" min="0" data-field="salary" placeholder="0.00">
        </div>
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_sellers">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_sellers').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showSellersMenu)]);
    });


    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} ${t('sellers.add')} — <strong>${escapeHtml(payload.username)}</strong>?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/sellers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('sellers.added')(escapeHtml(payload.username)));
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('sellers.add_more'), action: () => showAddSellerForm() }, catBack(showSellersMenu)]);
    });
  }

  // ---------------- Salary ----------------
  function showSalaryMenu() {
    state.currentCategory = 'salary';
    updateBackButtonVisibility();
    addBotMessage(t('salary.menu_title'));
    addOptions([
      { label: t('salary.view'), action: runListSalary },
      { label: t('salary.add'), action: () => showPaySalaryForm() },
      backButton(),
    ]);
  }

  async function runListSalary() {
    const typing = showTyping();
    const res = await apiCall('/assistant/api/salary');
    typing.remove();
    if (!res.ok) { addBotMessage((res.data && res.data.message) || res.message || t('generic_error')); addOptions([catBack(showSalaryMenu)]); return; }
    const payments = res.data.payments;
    const s = I18N[state.lang].salary;
    if (!payments.length) { addBotMessage(s.none); addOptions([catBack(showSalaryMenu)]); return; }
    addBotMessage(s.list_title(payments.length));
    addBotMessage(payments.map((p) =>
      `• <strong>${escapeHtml(p.employee)}</strong> — ${fmtMoney(p.amount)}${p.payment_date ? ' — ' + p.payment_date : ''}`
    ).join('<br>'), { speak: false });
    addOptions([catBack(showSalaryMenu)]);
  }

  async function showPaySalaryForm(prefill) {
    const f = I18N[state.lang].salary.form;
    const s = I18N[state.lang].salary;
    const p = prefill || {};

    const typing = showTyping();
    const sellersRes = await apiCall('/assistant/api/sellers');
    typing.remove();
    const sellers = (sellersRes.ok && sellersRes.data.sellers) || [];

    if (!sellers.length) {
      addBotMessage(s.no_employees);
      addOptions([catBack(showSalaryMenu)]);
      return;
    }

    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    const optionsHtml = [`<option value="" disabled ${p.employee ? '' : 'selected'}>${s.select_ph}</option>`]
      .concat(sellers.map((seller) => {
        const selected = p.employee && p.employee === seller.username ? 'selected' : '';
        return `<option value="${escapeHtml(seller.username)}" ${selected}>${escapeHtml(seller.username)} — ${fmtMoney(seller.salary)}</option>`;
      })).join('');
    form.innerHTML = `
      <div>
        <label>${f.employee}</label>
        <select data-field="employee">${optionsHtml}</select>
      </div>
      <div class="row2">
        <div>
          <label>${f.amount}</label>
          <input type="number" step="0.01" min="0" data-field="amount" placeholder="0.00" value="${p.amount != null ? p.amount : ''}">
        </div>
        <div>
          <label>${f.source}</label>
          <input type="text" data-field="source" placeholder="kasse">
        </div>
      </div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_salary">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();

    form.querySelector('#assistant_inline_back_salary').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showSalaryMenu)]);
    });


    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => { payload[el.dataset.field] = el.value; });

      if (!payload.employee) {
        addBotMessage(s.select_ph);
        return;
      }

      const confirmed = await confirmAction(
        `${t('confirm_prompt')} <strong>${escapeHtml(payload.employee)}</strong> — ${fmtMoney(payload.amount)}?`
      );
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/salary', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('salary.paid')(escapeHtml(payload.employee), fmtMoney(payload.amount)));
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('salary.add_more'), action: () => showPaySalaryForm() }, catBack(showSalaryMenu)]);
    });
  }

  // ---------------- Orders / Purchasing (Einkauf) ----------------
  function showOrdersMenu() {
    state.currentCategory = 'orders';
    updateBackButtonVisibility();
    addBotMessage(t('orders.menu_title'));
    addOptions([
      { label: t('orders.view'), action: runRecentOrders },
      canUseTool('add_order') ? { label: t('orders.add'), action: () => showAddOrderForm() } : null,
      backButton(),
    ].filter(Boolean));
  }

  async function runRecentOrders() {
    const o = I18N[state.lang].orders;
    const wrap = document.createElement('div');
    wrap.className = 'assistant-msg bot';
    wrap.innerHTML = `
      <div class="assistant-suggest-wrap" style="margin-bottom:0.5rem;">
        <input type="text" data-role="orders-search" autocomplete="off" placeholder="${o.search_ph}">
      </div>
      <div data-role="orders-results"></div>
    `;
    body.appendChild(wrap);
    scrollToBottom();
    const resultsEl = wrap.querySelector('[data-role="orders-results"]');
    const searchInput = wrap.querySelector('[data-role="orders-search"]');

    function renderRow(ord) {
      const row = document.createElement('div');
      row.style.marginBottom = '0.6rem';
      row.innerHTML = `• #${escapeHtml(String(ord.order_number))} <strong>${escapeHtml(ord.product_name)}</strong> — ${ord.quantity} × ${fmtMoney(ord.price)} = ${fmtMoney(ord.total_price)}${ord.date ? ' — ' + ord.date : ''}`;

      // Opens the existing purchase-order edit page — same route/controller
      // the web UI's Einkauf/Orders page uses.
      const editBtn = document.createElement('button');
      editBtn.className = 'assistant-option-btn';
      editBtn.style.marginTop = '0.4rem';
      editBtn.textContent = o.edit_btn(ord.order_number);
      editBtn.addEventListener('click', () => window.open(`/orders/${encodeURIComponent(ord.order_number)}/edit`, '_blank'));

      const delBtn = document.createElement('button');
      delBtn.className = 'assistant-option-btn danger';
      delBtn.style.marginTop = '0.4rem';
      delBtn.style.marginLeft = '0.4rem';
      delBtn.textContent = o.delete_btn;
      delBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(o.confirm_delete(escapeHtml(String(ord.order_number))));
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        editBtn.disabled = true;
        delBtn.disabled = true;
        delBtn.textContent = '…';
        const delRes = await apiCall(`/assistant/api/orders/${encodeURIComponent(ord.order_number)}/delete`, { method: 'POST' });
        if (delRes.ok && delRes.data.success) {
          row.style.opacity = '0.35';
          row.style.textDecoration = 'line-through';
          delBtn.textContent = o.deleted;
        } else {
          editBtn.disabled = false;
          delBtn.disabled = false;
          delBtn.textContent = o.delete_btn;
          addBotMessage((delRes.data && delRes.data.message) || delRes.message || t('generic_error'));
        }
      });

      row.appendChild(document.createElement('br'));
      if (ROLE === 'admin') row.appendChild(editBtn);
      if (ROLE === 'admin') row.appendChild(delBtn);
      resultsEl.appendChild(row);
    }

    async function renderResults(query) {
      resultsEl.innerHTML = `<span class="assistant-status-hint">${t('loading') || '…'}</span>`;
      const res = await apiCall(`/assistant/api/orders/recent${query ? '?q=' + encodeURIComponent(query) : ''}`);
      resultsEl.innerHTML = '';
      if (!res.ok) { resultsEl.textContent = (res.data && res.data.message) || res.message || t('generic_error'); return; }
      const orders = res.data.orders;
      if (!orders.length) { resultsEl.textContent = o.none; return; }
      const title = document.createElement('div');
      title.style.marginBottom = '0.5rem';
      title.innerHTML = o.list_title(orders.length);
      resultsEl.appendChild(title);
      orders.forEach(renderRow);
      scrollToBottom();
    }

    searchInput.addEventListener('input', debounce(() => renderResults(searchInput.value.trim()), 300));
    await renderResults('');
    addOptions([catBack(showOrdersMenu)]);
  }

  async function showAddOrderForm(prefill) {
    const f = I18N[state.lang].orders.form;
    const p = prefill || {};
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div>
        <label>${f.product_name}</label>
        <select data-field="product_name" data-mode="existing">
          <option value="">${f.product_loading || 'Loading products…'}</option>
        </select>
        <label class="assistant-inline-checkbox">
          <input type="checkbox" id="assistant_order_new_product"> ${f.new_product_toggle || 'This is a new product'}
        </label>
        <input type="text" data-field="product_name_new" placeholder="${f.product_name_ph}" style="display:none;">
      </div>
      <div class="row2">
        <div>
          <label>${f.price}</label>
          <input type="number" step="0.01" min="0" data-field="price" placeholder="0.00" value="${p.price != null ? p.price : ''}">
        </div>
        <div>
          <label>${f.selling_price}</label>
          <input type="number" step="0.01" min="0" data-field="selling_price" placeholder="0.00" value="${p.selling_price != null ? p.selling_price : ''}">
        </div>
      </div>
      <div class="row2">
        <div>
          <label>${f.quantity}</label>
          <select data-field="quantity">
            ${Array.from({length: 50}, (_, i) => i + 1).map(n => `<option value="${n}" ${n === 1 ? 'selected' : ''}>${n}</option>`).join('')}
          </select>
        </div>
        <div>
          <label>${f.ref_number}</label>
          <input type="text" data-field="ref_number" placeholder="${f.ref_number_ph}">
        </div>
      </div>

      <div>
        <label>${f.payment_method_label}</label>
        <select data-field="payment_method" id="assistant_order_payment_method">
          <option value="cash" selected>${f.payment_cash}</option>
          <option value="card" ${(!window.STRIPE_TERMINAL_CONFIG || !window.STRIPE_TERMINAL_CONFIG.configured) ? 'disabled' : ''}>${f.payment_card}</option>
        </select>
      </div>
      <div class="assistant-review-card d-none" id="assistant_order_card_status"></div>

      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_orders">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;

    body.appendChild(form);
    scrollToBottom();
    persistState();

    // Populate the product dropdown from the real catalog, same source
    // of truth as search_items()/db.search_items() — no free typing of a
    // product name that might not match anything real.
    const select = form.querySelector('select[data-field="product_name"]');
    try {
      const res = await apiCall('/assistant/api/items');
      const items = (res.ok && res.data.items) || [];
      select.innerHTML = `<option value="">${f.product_select_ph || 'Select a product…'}</option>` +
        items.map(it => `<option value="${escapeHtml(it.product_name)}" data-price="${it.purchase_price ?? ''}" data-selling="${it.selling_price ?? ''}">${escapeHtml(it.product_name)} (${it.quantity ?? 0} in stock)</option>`).join('');
      if (p.product_name) select.value = p.product_name;
    } catch (e) {
      select.innerHTML = `<option value="">${f.product_load_failed || 'Could not load products'}</option>`;
    }
    // Selecting an existing product pre-fills its known purchase/selling price.
    select.addEventListener('change', () => {
      const opt = select.selectedOptions[0];
      if (!opt) return;
      const priceInput = form.querySelector('[data-field="price"]');
      const sellingInput = form.querySelector('[data-field="selling_price"]');
      if (opt.dataset.price && !priceInput.value) priceInput.value = opt.dataset.price;
      if (opt.dataset.selling && !sellingInput.value) sellingInput.value = opt.dataset.selling;
    });

    const newToggle = form.querySelector('#assistant_order_new_product');
    const newNameInput = form.querySelector('[data-field="product_name_new"]');
    newToggle.addEventListener('change', () => {
      if (newToggle.checked) {
        select.style.display = 'none';
        newNameInput.style.display = '';
        newNameInput.focus();
      } else {
        select.style.display = '';
        newNameInput.style.display = 'none';
      }
    });

    form.querySelector('#assistant_inline_back_orders').addEventListener('click', () => {
      form.remove();
      // Keep UX: return to the previous menu state (Orders menu).
      addOptions([catBack(showOrdersMenu)]);
    });


    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      const payload = {};
      form.querySelectorAll('[data-field]').forEach((el) => {
        if (el.dataset.field === 'product_name_new') return; // merged below
        payload[el.dataset.field] = el.value;
      });
      payload.product_name = newToggle.checked ? newNameInput.value.trim() : select.value;

      if (!payload.product_name) {
        addBotMessage(f.product_required || 'Please select a product, or check "This is a new product" and type its name.');
        return;
      }
      if (!payload.price || !payload.selling_price || !payload.quantity) {
        addBotMessage(f.fields_required || 'Please fill in purchase price, selling price, and quantity.');
        return;
      }

      const totalPreview = (parseFloat(payload.price) * parseInt(payload.quantity, 10)).toFixed(2);
      const summary = `
        <div class="assistant-review-card">
          <div class="assistant-review-title">${f.review_title || 'Review purchase order'}</div>
          <div class="assistant-review-row"><span>${f.product_name}</span><strong>${escapeHtml(payload.product_name)}</strong></div>
          <div class="assistant-review-row"><span>${f.quantity}</span><strong>${payload.quantity}</strong></div>
          <div class="assistant-review-row"><span>${f.price}</span><strong>${fmtMoney(payload.price)}</strong></div>
          <div class="assistant-review-row"><span>${f.selling_price}</span><strong>${fmtMoney(payload.selling_price)}</strong></div>
          <div class="assistant-review-row"><span>${f.ref_number}</span><strong>${escapeHtml(payload.ref_number) || '—'}</strong></div>
          <div class="assistant-review-row total"><span>${f.total_cost || 'Total cost'}</span><strong>${fmtMoney(totalPreview)}</strong></div>
        </div>`;

      const confirmed = await confirmAction(summary);
      if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }

      const submitBtn = form.querySelector('.assistant-inline-submit');
      const cardStatusEl = form.querySelector('#assistant_order_card_status');

      function setCardStatus(text, isError) {
        if (!cardStatusEl) return;
        if (!text) { cardStatusEl.classList.add('d-none'); cardStatusEl.textContent = ''; return; }
        cardStatusEl.classList.remove('d-none');
        cardStatusEl.style.color = isError ? '#a3231d' : 'inherit';
        cardStatusEl.textContent = text;
      }

      // A "card" purchase order must actually be captured by the physical
      // Stripe Terminal reader first — exactly like a card sale or debt
      // payment — never just send payment_method: 'card' to the server
      // without a real, server-verified charge behind it.
      if (payload.payment_method === 'card') {
        if (!window.StripeTerminalPay || !window.STRIPE_TERMINAL_CONFIG || !window.STRIPE_TERMINAL_CONFIG.configured) {
          addBotMessage(`❌ ${f.card_not_configured}`);
          return;
        }
        submitBtn.disabled = true;
        const statusLabels = {
          reader_discovering: f.card_reader_discovering,
          reader_connecting: f.card_reader_connecting,
          creating_intent: f.card_creating_intent,
          present_card: f.card_present_card,
          processing: f.card_processing,
        };
        try {
          const paymentIntentId = await StripeTerminalPay.collectPayment(
            parseFloat(totalPreview), { context: 'purchase_order', reference: 'assistant-new-order' },
            { onStatus: (key) => setCardStatus(statusLabels[key] || key, false) }
          );
          payload.stripe_payment_intent_id = paymentIntentId;
          setCardStatus('', false);
        } catch (err) {
          setCardStatus('', false);
          addBotMessage(`❌ ${f.card_error_prefix} ${err.message}`);
          submitBtn.disabled = false;
          return;
        }
      }

      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      let createdOrderNumber = null;
      if (res.ok && res.data.success) {
        const d = res.data;
        createdOrderNumber = d.order_number;
        addBotMessage(`
          <div class="assistant-review-card success">
            <div class="assistant-review-title">✅ ${f.order_confirmed || 'Purchase order created'}</div>
            <div class="assistant-review-row"><span>${f.product_name}</span><strong>${escapeHtml(payload.product_name)}</strong></div>
            <div class="assistant-review-row"><span>${f.quantity}</span><strong>${payload.quantity}</strong></div>
            <div class="assistant-review-row total"><span>${f.total_cost || 'Total cost'}</span><strong>${fmtMoney(d.total_price)}</strong></div>
            ${d.order_number ? `<div class="assistant-review-row"><span>${f.order_number || 'Order #'}</span><strong>${escapeHtml(String(d.order_number))}</strong></div>` : ''}
          </div>`, { speak: false });
      } else {
        addBotMessage(`❌ ${(res.data && res.data.message) || res.message || t('generic_error')}`);
      }
      // The order's own order_number IS the newly-stocked product's barcode
      // (see add_order() in db.py) — offer to print it straight away instead
      // of making the person go find it in Items afterwards.
      addOptions([
        ...(createdOrderNumber ? [{
          label: I18N[state.lang].sell.print_barcode,
          action: () => showItemBarcode(createdOrderNumber, payload.product_name, showOrdersMenu),
        }] : []),
        { label: t('orders.add_more'), action: () => showAddOrderForm() },
        catBack(showOrdersMenu),
      ]);
    });
  }



  // ---------------- Sales (Meine Verkäufe) ----------------
  function showSalesMenu() {
    state.currentCategory = 'sales';
    updateBackButtonVisibility();
    addBotMessage(t('sales.menu_title'));
    addOptions([
      { label: t('sales.view'), action: runRecentSales },
      backButton(),
    ]);
  }

  async function runRecentSales() {
    const s = I18N[state.lang].sales;
    const wrap = document.createElement('div');
    wrap.className = 'assistant-msg bot';
    wrap.innerHTML = `
      <div class="assistant-suggest-wrap" style="margin-bottom:0.5rem;">
        <input type="text" data-role="sales-search" autocomplete="off" placeholder="${s.search_ph}">
      </div>
      <div data-role="sales-results"></div>
    `;
    body.appendChild(wrap);
    scrollToBottom();
    const resultsEl = wrap.querySelector('[data-role="sales-results"]');
    const searchInput = wrap.querySelector('[data-role="sales-search"]');

    function renderRow(sale, displayIndex) {
      const row = document.createElement('div');
      row.style.marginBottom = '0.6rem';
      row.innerHTML = `• ${s.row(displayIndex, fmtMoney(sale.total), sale.items_count)}${sale.date ? ' — ' + sale.date : ''}`;
      row.appendChild(document.createElement('br'));

      // Each line item is referenced by its barcode (scoped to this sale's
      // order_id, since the same barcode can appear across many different
      // sales) — never by the internal sale_items.id — matching how Items
      // and Orders are already referenced. The server resolves the real
      // row itself.
      (sale.items || []).forEach((it) => {
        if (ROLE !== 'admin') return; // edit/delete-per-line-item are admin-only server-side
        const itemLabel = sale.items.length > 1 ? `${it.product_name} (#${it.barcode})` : null;

        const editBtn = document.createElement('button');
        editBtn.className = 'assistant-option-btn';
        editBtn.style.marginTop = '0.4rem';
        editBtn.style.marginRight = '0.4rem';
        editBtn.textContent = itemLabel ? `${s.edit_btn} — ${itemLabel}` : s.edit_btn;
        editBtn.addEventListener('click', async () => {
          editBtn.disabled = true;
          const res = await apiCall(`/assistant/api/sales/${encodeURIComponent(sale.order_id)}/item/${encodeURIComponent(it.barcode)}/edit-url`);
          editBtn.disabled = false;
          if (res.ok && res.data.success) {
            window.open(res.data.edit_url, '_blank');
          } else {
            addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
          }
        });
        row.appendChild(editBtn);

        const delItemBtn = document.createElement('button');
        delItemBtn.className = 'assistant-option-btn danger';
        delItemBtn.style.marginTop = '0.4rem';
        delItemBtn.style.marginRight = '0.4rem';
        delItemBtn.textContent = itemLabel ? `${s.delete_btn} — ${itemLabel}` : s.delete_btn;
        delItemBtn.addEventListener('click', async () => {
          const confirmed = await confirmAction(s.delete_confirm(displayIndex));
          if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
          editBtn.disabled = true;
          delItemBtn.disabled = true;
          delItemBtn.textContent = '…';
          const delRes = await apiCall(`/assistant/api/sales/${encodeURIComponent(sale.order_id)}/item/${encodeURIComponent(it.barcode)}`, { method: 'DELETE' });
          if (delRes.ok && delRes.data.success) {
            editBtn.style.display = 'none';
            delItemBtn.textContent = s.deleted;
            delItemBtn.disabled = true;
            if (sale.items.length === 1) {
              row.style.opacity = '0.35';
              row.style.textDecoration = 'line-through';
            }
          } else {
            editBtn.disabled = false;
            delItemBtn.disabled = false;
            delItemBtn.textContent = itemLabel ? `${s.delete_btn} — ${itemLabel}` : s.delete_btn;
            addBotMessage((delRes.data && delRes.data.message) || delRes.message || t('generic_error'));
          }
        });
        row.appendChild(delItemBtn);
      });
      resultsEl.appendChild(row);
    }

    async function renderResults(query) {
      resultsEl.innerHTML = `<span class="assistant-status-hint">${t('loading') || '…'}</span>`;
      const res = await apiCall(`/assistant/api/sales/recent${query ? '?q=' + encodeURIComponent(query) : ''}`);
      resultsEl.innerHTML = '';
      if (!res.ok) { resultsEl.textContent = (res.data && res.data.message) || res.message || t('generic_error'); return; }
      const sales = res.data.sales;
      if (!sales.length) { resultsEl.textContent = s.none; return; }
      const title = document.createElement('div');
      title.style.marginBottom = '0.5rem';
      title.innerHTML = s.list_title(sales.length);
      resultsEl.appendChild(title);
      sales.forEach((sale, idx) => renderRow(sale, idx + 1));
      scrollToBottom();
    }

    searchInput.addEventListener('input', debounce(() => renderResults(searchInput.value.trim()), 300));
    await renderResults('');
    addOptions([catBack(showSalesMenu)]);
  }

  // ---------------- Sell (Verkaufen — quick single-item sale) ----------------
  function showSellMenu() {
    state.currentCategory = 'sell';
    updateBackButtonVisibility();
    addBotMessage(t('sell.menu_title'));
    addOptions([
      { label: t('sell.quick'), action: () => showQuickSellForm() },
      backButton(),
    ]);
  }

  // Small debounce so the item lookup fires once typing pauses, not on
  // every keystroke.
  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function showQuickSellForm(prefill) {
    const f = I18N[state.lang].sell.form;
    const p = prefill || {};
    const form = document.createElement('div');
    form.className = 'assistant-inline-form';
    form.innerHTML = `
      <div class="assistant-suggest-wrap">
        <label>${f.identifier}</label>
        <input type="text" data-field="identifier" autocomplete="off" placeholder="${f.identifier_ph}" value="${escapeHtml(p.identifier || '')}">
        <div class="assistant-suggest-list" style="display:none;"></div>
      </div>
      <div class="row2">
        <div>
          <label>${f.quantity}</label>
          <input type="number" step="1" min="1" data-field="quantity" placeholder="1" value="${p.quantity != null ? p.quantity : '1'}">
        </div>
        <div>
          <label>&nbsp;</label>
          <div class="assistant-inline-hint" data-role="preview">&nbsp;</div>
        </div>
      </div>
      <div class="assistant-inline-hint assistant-inline-error" data-role="stock-warning" style="display:none;"></div>
      <div>
        <label>${f.payment_method_label}</label>
        <select data-field="payment_method" id="assistant_sell_payment_method">
          <option value="cash" selected>${f.payment_cash}</option>
          <option value="card" ${(!window.STRIPE_TERMINAL_CONFIG || !window.STRIPE_TERMINAL_CONFIG.configured) ? 'disabled' : ''}>${f.payment_card}</option>
        </select>
      </div>
      <div class="assistant-review-card d-none" id="assistant_sell_card_status"></div>
      <div class="row2" style="gap:0.5rem;">
        <button type="button" class="assistant-inline-back" id="assistant_inline_back_sell">${t('back')}</button>
        <button class="assistant-inline-submit">${f.save}</button>
      </div>
    `;
    body.appendChild(form);
    scrollToBottom();
    persistState();

    const identifierInput = form.querySelector('[data-field="identifier"]');
    const quantityInput = form.querySelector('[data-field="quantity"]');
    const suggestList = form.querySelector('.assistant-suggest-list');
    const previewEl = form.querySelector('[data-role="preview"]');
    const stockWarningEl = form.querySelector('[data-role="stock-warning"]');
    const submitBtn = form.querySelector('.assistant-inline-submit');
    let selectedBarcode = null; // set once a suggestion is picked, so we sell the exact item
    let selectedItem = null; // full item record for the current selection, used for the live preview

    // "Change my mind and go back" — leaves the sell menu without submitting
    // anything, same behaviour as every other inline form in this assistant.
    form.querySelector('#assistant_inline_back_sell').addEventListener('click', () => {
      form.remove();
      addOptions([catBack(showSellMenu)]);
    });

    function hideSuggestions() {
      suggestList.style.display = 'none';
      suggestList.innerHTML = '';
    }

    // Smart live preview: as soon as a real product + a valid quantity are
    // known, show the total price and warn early (before the person even
    // tries to submit) if the requested quantity exceeds stock on hand.
    function updatePreview() {
      const qty = parseInt(quantityInput.value, 10);
      if (!selectedItem || !qty || qty <= 0) {
        previewEl.innerHTML = '&nbsp;';
        stockWarningEl.style.display = 'none';
        submitBtn.disabled = false;
        return;
      }
      previewEl.textContent = `= ${fmtMoney(selectedItem.selling_price * qty)}`;
      if (qty > selectedItem.quantity) {
        stockWarningEl.style.display = 'block';
        stockWarningEl.textContent = t('sell.not_enough_stock')(selectedItem.quantity);
        submitBtn.disabled = true;
      } else {
        stockWarningEl.style.display = 'none';
        submitBtn.disabled = false;
      }
    }

    function renderSuggestions(items) {
      if (!items.length) {
        suggestList.innerHTML = `<div class="assistant-suggest-item" style="cursor:default;color:var(--ink-soft);">${t('sell.none')}</div>`;
        suggestList.style.display = 'block';
        return;
      }
      suggestList.innerHTML = '';
      items.forEach((item) => {
        const row = document.createElement('div');
        row.className = 'assistant-suggest-item';
        const warning = item.quantity <= 5 ? '⚠️ ' : '';
        row.innerHTML = `
          <span class="name">${warning}${escapeHtml(item.product_name)}</span>
          <span class="meta">${fmtMoney(item.selling_price)} · ${item.quantity}×</span>
        `;
        row.addEventListener('click', () => {
          identifierInput.value = item.product_name;
          selectedBarcode = item.barcode;
          selectedItem = item;
          hideSuggestions();
          quantityInput.focus();
          updatePreview();
        });
        suggestList.appendChild(row);
      });
      suggestList.style.display = 'block';
    }

    const lookupItems = debounce(async (query) => {
      if (!query || query.length < 1) { hideSuggestions(); return; }
      const res = await apiCall(`/assistant/api/items?q=${encodeURIComponent(query)}`);
      if (!res.ok) { hideSuggestions(); return; }
      renderSuggestions((res.data.items || []).slice(0, 8));
    }, 250);

    identifierInput.addEventListener('input', () => {
      selectedBarcode = null; // typed manually again — no longer bound to a suggestion
      selectedItem = null;
      updatePreview();
      lookupItems(identifierInput.value.trim());
    });
    identifierInput.addEventListener('focus', () => {
      if (identifierInput.value.trim()) lookupItems(identifierInput.value.trim());
    });
    quantityInput.addEventListener('input', updatePreview);
    document.addEventListener('click', (e) => {
      if (!form.contains(e.target)) hideSuggestions();
    });

    if (p.identifier) updatePreview();

    form.querySelector('.assistant-inline-submit').addEventListener('click', async () => {
      hideSuggestions();
      const paymentMethodSelect = form.querySelector('[data-field="payment_method"]');
      const payload = {
        identifier: selectedBarcode || identifierInput.value,
        quantity: quantityInput.value,
        payment_method: paymentMethodSelect.value,
      };

      const previewText = selectedItem
        ? `${escapeHtml(payload.identifier)} x${payload.quantity} (${fmtMoney(selectedItem.selling_price * (parseInt(payload.quantity, 10) || 0))})`
        : `${escapeHtml(payload.identifier)} x${payload.quantity}`;
      const confirmed = await confirmAction(`${t('confirm_prompt')} ${previewText}?`);
      if (!confirmed) {
        // Same behaviour as every other inline form: cancelling the
        // confirmation just leaves the form on screen with the values the
        // person already typed, so they can change their mind and adjust
        // the quantity/product instead of starting over from scratch.
        addBotMessage(t('confirm_cancelled'));
        return;
      }

      const cardStatusEl = form.querySelector('#assistant_sell_card_status');
      function setCardStatus(text, isError) {
        if (!cardStatusEl) return;
        if (!text) { cardStatusEl.classList.add('d-none'); cardStatusEl.textContent = ''; return; }
        cardStatusEl.classList.remove('d-none');
        cardStatusEl.style.color = isError ? '#a3231d' : 'inherit';
        cardStatusEl.textContent = text;
      }

      // A "card" sale must be captured by the physical Stripe Terminal (TPE)
      // reader first — same rule as purchase orders and debt payments —
      // never just tell the server "card" without a real payment_intent_id
      // behind it, since the server re-verifies it against Stripe anyway.
      if (payload.payment_method === 'card') {
        if (!window.StripeTerminalPay || !window.STRIPE_TERMINAL_CONFIG || !window.STRIPE_TERMINAL_CONFIG.configured) {
          addBotMessage(`❌ ${f.card_not_configured}`);
          return;
        }
        if (!selectedItem) {
          addBotMessage(f.card_error_prefix + ' ' + (f.identifier));
          return;
        }
        const total = selectedItem.selling_price * (parseInt(payload.quantity, 10) || 0);
        submitBtn.disabled = true;
        const statusLabels = {
          reader_discovering: f.card_reader_discovering,
          reader_connecting: f.card_reader_connecting,
          creating_intent: f.card_creating_intent,
          present_card: f.card_present_card,
          processing: f.card_processing,
        };
        try {
          const paymentIntentId = await StripeTerminalPay.collectPayment(
            total, { context: 'sale', reference: 'assistant-quick-sell' },
            { onStatus: (key) => setCardStatus(statusLabels[key] || key, false) }
          );
          payload.stripe_payment_intent_id = paymentIntentId;
          setCardStatus('', false);
        } catch (err) {
          setCardStatus('', false);
          addBotMessage(`❌ ${f.card_error_prefix} ${err.message}`);
          submitBtn.disabled = false;
          return;
        }
      }

      submitBtn.disabled = true;
      submitBtn.textContent = f.saving;

      const res = await apiCall('/assistant/api/sell', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      form.remove();
      if (res.ok && res.data.success) {
        addBotMessage(t('sell.sold')(res.data.quantity, escapeHtml(res.data.product_name), fmtMoney(res.data.total_price)));
        if (res.data.low_stock) addBotMessage(t('sell.low_stock')(res.data.remaining));
        const soldBarcode = res.data.barcode;
        const soldName = res.data.product_name;
        addOptions([
          { label: t('sell.quick'), action: () => showQuickSellForm() },
          ...(soldBarcode ? [{ label: t('sell.print_barcode'), action: () => showItemBarcode(soldBarcode, soldName) }] : []),
          catBack(showSellMenu),
        ]);
        return;
      } else {
        addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      }
      addOptions([{ label: t('sell.quick'), action: () => showQuickSellForm() }, catBack(showSellMenu)]);
    });
  }

  // Fetches and displays a printable barcode image for an item, with a
  // button that opens the browser print dialog on just that image.
  async function showItemBarcode(barcodeValue, productName, backAction) {
    const back = backAction || showSellMenu;
    const typing = showTyping();
    const res = await apiCall(`/assistant/api/items/${encodeURIComponent(barcodeValue)}/barcode`);
    typing.remove();
    if (!res.ok || !res.data.success) {
      addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      addOptions([catBack(back)]);
      return;
    }
    const s = I18N[state.lang].sell;
    const wrap = document.createElement('div');
    wrap.className = 'assistant-msg bot';
    wrap.innerHTML = `
      <div>${s.barcode_title(escapeHtml(productName || res.data.product_name || ''))}</div>
      <img src="${res.data.image_url}" alt="barcode" style="max-width:100%;margin-top:0.5rem;background:#fff;padding:0.5rem;border-radius:6px;">
    `;
    body.appendChild(wrap);
    scrollToBottom();
    persistState();
    addOptions([
      { label: s.print_btn, action: () => { const w = window.open(res.data.print_url, '_blank'); if (w) w.onload = () => w.print(); addOptions([catBack(back)]); } },
      catBack(back),
    ]);
  }

  // Registry used to resume the right screen when the chat is restored
  // from localStorage after navigating to a different page.
  const CATEGORY_ENTRY = {
    main: showMainMenu,
    report: showReportMenu,
    debts: showDebtsMenu,
    factures: showFacturesMenu,
    stock: showStockMenu,
    kasse: showKasseMenu,
    items: showItemsMenu,
    sellers: showSellersMenu,
    salary: showSalaryMenu,
    orders: showOrdersMenu,
    sales: showSalesMenu,
    sell: showSellMenu,
  };

  // ---------------------------------------------------------------------
  // Free-text / voice NLU
  // ---------------------------------------------------------------------
  function containsAny(text, words) {
    return fuzzyContainsAny(text, words);
  }

  // Pull the first plausible amount (e.g. "50", "50.00", "50,00") out of text.
  function extractAmount(text) {
    const m = text.match(/(\d+(?:[.,]\d{1,2})?)/);
    if (!m) return null;
    return parseFloat(m[1].replace(',', '.'));
  }

  // Best-effort name extraction: strip known keywords/numbers, keep the rest.
  function extractName(rawText, lang) {
    const kw = KEYWORDS[lang];
    let words = rawText.split(/\s+/).filter(Boolean);
    const stop = new Set([
      ...kw.add, ...kw.pay, ...kw.debts, ...kw.factures, ...kw.show,
      ...kw.items, ...kw.sellers, ...kw.salary, ...kw.kasse,
      ...kw.orders, ...kw.sales, ...kw.sell,
      'a', 'an', 'the', 'for', 'debt', 'دين', 'لـ', 'für', 'schuld', 'eine',
    ]);
    words = words.filter((w) => !stop.has(w.toLowerCase()) && !/^\d+([.,]\d+)?$/.test(w) && w !== '€');
    return words.join(' ').trim();
  }

  // Fast local shortcut for the three most common one-line commands ("pay
  // Ahmed's debt", "add debt for Sara 50", "pay Sara's salary 500") — matches
  // by keyword instead of a network round-trip to the AI. Anything that
  // isn't a confident match (ambiguous wording, a "show/list" phrasing, or a
  // name/amount we can't extract) falls through and is left to the AI, so
  // this can only ever make simple commands faster, never break anything
  // more complex.
  async function tryLocalIntent(text) {
    const lang = state.lang;
    const kw = KEYWORDS[lang];
    const lower = text.toLowerCase();

    // A "show/list" phrasing (e.g. "show paid debts") is a read, not an
    // action — always defer those to the AI/menus rather than risk firing
    // a payment off a listing question.
    if (containsAny(lower, kw.show)) return false;

    const hasPay = containsAny(lower, kw.pay);
    const hasAdd = containsAny(lower, kw.add);
    const hasDebt = containsAny(lower, kw.debts);
    const hasSalary = containsAny(lower, kw.salary);

    if (hasAdd && hasDebt) {
      const name = extractName(text, lang);
      const amount = extractAmount(text);
      if (name || amount != null) {
        await handleAddDebtIntent(name, amount);
        return true;
      }
    }

    if (hasPay && hasDebt) {
      const name = extractName(text, lang);
      if (name) {
        await handlePayDebtIntent(name);
        return true;
      }
    }

    if (hasPay && hasSalary) {
      const employee = extractName(text, lang);
      const amount = extractAmount(text);
      if (employee) {
        await handlePaySalaryIntent(employee, amount);
        return true;
      }
    }

    return false;
  }

  async function handlePayDebtIntent(name) {
    const typing = showTyping();
    const { debts, message } = await fetchOpenDebts();
    typing.remove();
    if (!debts) { addBotMessage(message || t('generic_error')); addOptions([catBack(showDebtsMenu)]); return; }
    const match = debts.find((d) => d.client_name && d.client_name.toLowerCase().includes(name.toLowerCase()));
    if (!match) { addBotMessage(t('debts.not_found')(escapeHtml(name))); addOptions([catBack(showDebtsMenu)]); return; }
    const f = I18N[state.lang].debts.form;
    const confirmed = await confirmAction(
      buildReviewCard(t('debts.mark_paid'), [
        [f.client, escapeHtml(match.client_name)],
        [f.amount, fmtMoney(match.amount)],
      ])
    );
    if (!confirmed) { addBotMessage(t('confirm_cancelled')); addOptions([catBack(showDebtsMenu)]); return; }
    const payment = await collectPaymentMethod(match.amount, match.debt_id, 'debts');
    const payRes = await apiCall(`/assistant/api/debts/${encodeURIComponent(match.debt_id)}/pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payment_method: payment.method, stripe_payment_intent_id: payment.stripePaymentIntentId }),
    });
    if (payRes.ok) {
      addBotMessage(t('debts.paid_voice')(escapeHtml(match.client_name)));
    } else {
      addBotMessage((payRes.data && payRes.data.message) || payRes.message || t('generic_error'));
    }
    addOptions([catBack(showDebtsMenu)]);
  }

  async function handleAddDebtIntent(name, amount) {
    if (!name || amount == null || isNaN(amount)) {
      addBotMessage(t('debts.need_info'));
      showAddDebtForm({ name, amount });
      return;
    }
    const debtsF = I18N[state.lang].debts.form;
    const confirmed = await confirmAction(
      buildReviewCard(t('debts.add'), [
        [debtsF.client, escapeHtml(name)],
        [debtsF.amount, fmtMoney(amount)],
      ])
    );
    if (!confirmed) { addBotMessage(t('confirm_cancelled')); addOptions([catBack(showDebtsMenu)]); return; }
    const res = await apiCall('/assistant/api/debts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_name: name, amount }),
    });
    if (res.ok && res.data.success) {
      addBotMessage(t('debts.added')(escapeHtml(name), fmtMoney(amount)));
      if (res.data.reference_number) {
        addBotMessage(`${I18N[state.lang].debts.form.reference}: <strong>${escapeHtml(res.data.reference_number)}</strong>`, { speak: false });
      }
      addOptions([catBack(showDebtsMenu)]);
    } else {
      addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
      addOptions([catBack(showDebtsMenu)]);
    }
  }

  // Guess which of the three supported languages a free-typed / spoken
  // message is in, so the single chat can answer in kind without the
  // person ever having to pick a language. Arabic script is unambiguous;
  // German vs English is scored from umlauts + a short common-word list +
  // overlap with the domain keyword sets. Returns null when inconclusive
  // (message too short / neutral), in which case the current language
  // is kept.
  function detectLanguage(text) {
    if (/[\u0600-\u06FF]/.test(text)) return 'ar';
    const lower = text.toLowerCase();
    const commonWords = {
      de: ['der', 'die', 'das', 'und', 'ich', 'ist', 'nicht', 'ein', 'eine', 'wie', 'was',
           'bitte', 'danke', 'heute', 'möchte', 'kann', 'für', 'mit', 'auf'],
      en: ['the', 'and', 'is', 'not', 'a', 'an', 'how', 'what', 'please', 'thanks',
           'today', 'would', 'can', 'for', 'with', 'show', 'me'],
    };
    let deScore = 0;
    let enScore = 0;
    if (/[äöüß]/.test(lower)) deScore += 2;
    lower.split(/\s+/).forEach((tok) => {
      if (commonWords.de.includes(tok)) deScore += 1;
      if (commonWords.en.includes(tok)) enScore += 1;
    });
    ['de', 'en'].forEach((lang) => {
      Object.values(KEYWORDS[lang]).forEach((list) => {
        if (containsAny(lower, list)) { if (lang === 'de') deScore += 1; else enScore += 1; }
      });
    });
    if (deScore === 0 && enScore === 0) return null;
    return deScore >= enScore ? 'de' : 'en';
  }

  // Rolling chat history sent to the AI endpoint so it has context across
  // turns (e.g. to know what it's confirming). The server is now the real
  // source of truth (see /assistant/api/history) — this in-memory array is
  // just a fast local cache seeded from there on open, so the assistant
  // still remembers the conversation after a reload or on another device,
  // not just within one tab session.
  let aiChatHistory = [];
  let aiHistoryLoaded = false;

  async function ensureHistoryLoaded() {
    if (aiHistoryLoaded) return;
    aiHistoryLoaded = true;
    try {
      const url = state.conversationId
        ? `/assistant/api/history?conversation_id=${encodeURIComponent(state.conversationId)}`
        : '/assistant/api/history';
      const res = await apiCall(url);
      if (res.ok && res.data && res.data.success) {
        if (Array.isArray(res.data.history)) aiChatHistory = res.data.history.slice(-20);
        if (res.data.conversation_id) { state.conversationId = res.data.conversation_id; persistState(); }
      }
    } catch (e) {
      // No connection yet / not logged in (e.g. anonymous standalone
      // preview) — fine, chat still works, it just starts empty.
    }
  }

  // Starts a fresh conversation (like ChatGPT's "New chat") — the current
  // one is NOT deleted, it just stops being the active one; it'll show up
  // in the History list from now on.
  async function startNewConversation() {
    const res = await apiCall('/assistant/api/conversations/new', { method: 'POST' });
    if (res.ok && res.data && res.data.success) {
      state.conversationId = res.data.conversation_id;
    } else {
      state.conversationId = null; // worst case: next message just lands in a server-assigned conversation
    }
    aiChatHistory = [];
    aiHistoryLoaded = true; // we already know this conversation is empty — no need to fetch it
    body.innerHTML = '';
    persistState();
    addBotMessage(t('new_chat_started'));
    showMainMenu(false);
  }

  // Switches the active conversation to a past one and replays its
  // messages into the chat body, so the person can keep talking in it
  // exactly where they left off — not just a read-only transcript.
  async function loadConversation(conversationId) {
    state.conversationId = conversationId;
    aiHistoryLoaded = false;
    body.innerHTML = '';
    await ensureHistoryLoaded();
    aiChatHistory.forEach((turn) => {
      if (turn.role === 'user') addUserMessage(turn.content);
      else addBotMessage(escapeHtml(turn.content).replace(/\n/g, '<br>'), { speak: false });
    });
    persistState();
    showMainMenu(false);
  }

  async function showConversationHistory() {
    const wrap = document.createElement('div');
    wrap.className = 'assistant-msg bot';
    wrap.innerHTML = `<div style="margin-bottom:0.5rem;"><strong>${t('history_title')}</strong></div><div data-role="history-results"></div>`;
    body.appendChild(wrap);
    scrollToBottom();
    const resultsEl = wrap.querySelector('[data-role="history-results"]');
    resultsEl.innerHTML = `<span class="assistant-status-hint">${t('loading') || '…'}</span>`;

    const res = await apiCall('/assistant/api/conversations');
    resultsEl.innerHTML = '';
    if (!res.ok || !res.data || !res.data.success) {
      resultsEl.textContent = (res.data && res.data.message) || res.message || t('generic_error');
      addOptions([catBack(showMainMenu)]);
      return;
    }
    const conversations = res.data.conversations || [];
    if (!conversations.length) {
      resultsEl.textContent = t('history_none');
      addOptions([catBack(showMainMenu)]);
      return;
    }
    conversations.forEach((c) => {
      const row = document.createElement('div');
      row.style.marginBottom = '0.6rem';
      const when = c.last_message_at ? new Date(c.last_message_at).toLocaleString(state.lang === 'de' ? 'de-DE' : state.lang === 'ar' ? 'ar' : 'en-GB', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
      const isActive = c.conversation_id === state.conversationId;
      row.innerHTML = `${isActive ? '🟢 ' : ''}<strong>${escapeHtml(c.title || t('history_untitled'))}</strong><br><span style="opacity:0.7;font-size:0.85em;">${when}${c.message_count ? ' · ' + c.message_count : ''}</span>`;
      row.appendChild(document.createElement('br'));

      const openBtn = document.createElement('button');
      openBtn.className = 'assistant-option-btn';
      openBtn.style.marginTop = '0.4rem';
      openBtn.style.marginRight = '0.4rem';
      openBtn.textContent = t('history_load');
      openBtn.addEventListener('click', () => loadConversation(c.conversation_id));
      row.appendChild(openBtn);

      const delBtn = document.createElement('button');
      delBtn.className = 'assistant-option-btn danger';
      delBtn.style.marginTop = '0.4rem';
      delBtn.textContent = t('history_delete');
      delBtn.addEventListener('click', async () => {
        const confirmed = await confirmAction(t('history_confirm_delete'));
        if (!confirmed) { addBotMessage(t('confirm_cancelled')); return; }
        openBtn.disabled = true;
        delBtn.disabled = true;
        const delRes = await apiCall(`/assistant/api/conversations/${encodeURIComponent(c.conversation_id)}/delete`, { method: 'POST' });
        if (delRes.ok && delRes.data && delRes.data.success) {
          row.style.opacity = '0.35';
          row.style.textDecoration = 'line-through';
          if (isActive) startNewConversation();
        } else {
          openBtn.disabled = false;
          delBtn.disabled = false;
        }
      });
      row.appendChild(delBtn);
      resultsEl.appendChild(row);
    });
    addOptions([catBack(showMainMenu)]);
  }

  if (newChatBtn) newChatBtn.addEventListener('click', startNewConversation);
  if (historyBtn) historyBtn.addEventListener('click', showConversationHistory);

  // Friendly one-liners shown briefly while a tool call is in flight during
  // a streamed reply, so the chat reads as "doing something specific"
  // instead of a generic spinner. Falls back to a generic label for any
  // tool not listed here (new tools added server-side don't need a
  // matching entry to work, they just show something less tailored).
  const TOOL_STATUS_LABELS = {
    de: {
      get_summary: 'Schaue mir die Zahlen von heute an…', get_dashboard_stats: 'Werte das Dashboard aus…',
      list_items: 'Suche im Lagerbestand…', list_low_stock: 'Prüfe niedrigen Bestand…',
      list_open_debts: 'Sehe die offenen Schulden nach…', list_clients: 'Lade die Kundenliste…',
      list_unpaid_factures: 'Prüfe offene Rechnungen…', list_recent_sales: 'Sehe die letzten Verkäufe nach…',
      list_recent_orders: 'Sehe die letzten Einkäufe nach…', get_kasse_today: 'Prüfe den Kassenstand…',
      quick_sell: 'Trage den Verkauf ein…', search_audit_log: 'Durchsuche das Aktivitätsprotokoll…',
      remember_note: 'Merke mir das…', _default: 'Einen Moment…',
    },
    en: {
      get_summary: "Checking today's numbers…", get_dashboard_stats: 'Pulling up the dashboard…',
      list_items: 'Searching stock…', list_low_stock: 'Checking low stock…',
      list_open_debts: 'Looking up open debts…', list_clients: 'Loading the client list…',
      list_unpaid_factures: 'Checking unpaid invoices…', list_recent_sales: 'Looking up recent sales…',
      list_recent_orders: 'Looking up recent orders…', get_kasse_today: "Checking today's cash balance…",
      quick_sell: 'Recording the sale…', search_audit_log: 'Searching the activity log…',
      remember_note: 'Making a note of that…', _default: 'One moment…',
    },
    ar: {
      get_summary: 'أتحقق من أرقام اليوم…', get_dashboard_stats: 'أجلب لوحة التحكم…',
      list_items: 'أبحث في المخزون…', list_low_stock: 'أتحقق من المخزون المنخفض…',
      list_open_debts: 'أراجع الديون المفتوحة…', list_clients: 'أحمّل قائمة العملاء…',
      list_unpaid_factures: 'أتحقق من الفواتير غير المدفوعة…', list_recent_sales: 'أراجع آخر المبيعات…',
      list_recent_orders: 'أراجع آخر المشتريات…', get_kasse_today: 'أتحقق من رصيد الصندوق…',
      quick_sell: 'أسجل عملية البيع…', search_audit_log: 'أبحث في سجل النشاط…',
      remember_note: 'أدوّن هذا…', _default: 'لحظة من فضلك…',
    },
  };
  function toolStatusLabel(toolName) {
    const table = TOOL_STATUS_LABELS[state.lang] || TOOL_STATUS_LABELS.en;
    return table[toolName] || table._default;
  }

  async function processUserText(rawText) {
    const text = rawText.trim();
    if (!text) return;

    // Still guess the language client-side, just to switch the chat's own
    // UI chrome (placeholder text, mic language, RTL layout) instantly.
    // The actual reply's language is decided by the AI from the message
    // it receives, so it can't get "stuck" out of sync with the chrome.
    const detected = detectLanguage(text);
    if (detected && detected !== state.lang) applyLanguage(detected);

    // Try the fast local keyword shortcut first ("pay X's debt", "add debt
    // for X", "pay X's salary") — only fires on a confident match, so
    // anything else (including plain chit-chat) falls through to the AI
    // below exactly as before.
    if (await tryLocalIntent(text)) return;

    await ensureHistoryLoaded();

    const fallbackMenu = () => addOptions([
      { label: t('menu.report'), action: showReportMenu },
      { label: t('menu.sell'), action: showSellMenu },
      { label: t('menu.debts'), action: showDebtsMenu },
      { label: t('menu.factures'), action: showFacturesMenu },
      { label: t('menu.items'), action: showItemsMenu },
      { label: t('menu.orders'), action: showOrdersMenu },
      { label: t('menu.stock'), action: showStockMenu },
      { label: t('menu.sales'), action: showSalesMenu },
      { label: t('menu.sellers'), action: showSellersMenu },
      { label: t('menu.salary'), action: showSalaryMenu },
      { label: t('menu.kasse'), action: showKasseMenu },
    ]);

    const streamed = await streamAssistantReply(text, fallbackMenu);
    if (streamed) return;

    // Streaming unavailable/failed (older browser, proxy that buffers SSE,
    // etc.) — fall back to the original one-shot request so the chat still
    // works, just without the token-by-token feel.
    const typing = showTyping();
    const res = await apiCall('/assistant/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: aiChatHistory, lang: state.lang, conversation_id: state.conversationId }),
    });
    typing.remove();

    if (!res.ok || !res.data || !res.data.success) {
      addBotMessage((res.data && res.data.message) || res.message || t('connection_error'));
      fallbackMenu();
      return;
    }

    aiChatHistory.push({ role: 'user', content: text });
    aiChatHistory.push({ role: 'assistant', content: res.data.reply });
    if (aiChatHistory.length > 20) aiChatHistory = aiChatHistory.slice(-20);

    addBotMessage(escapeHtml(res.data.reply).replace(/\n/g, '<br>'));
    (res.data.widgets || (res.data.widget ? [res.data.widget] : [])).forEach(renderWidget);
  }

  // Streams the reply from /assistant/api/chat/stream (Server-Sent Events)
  // and renders it into the chat bubble as it arrives, instead of waiting
  // for the whole message. Returns true if the turn was handled this way
  // (success or a clean server-side error), false if streaming itself
  // couldn't be used at all and the caller should fall back.
  async function streamAssistantReply(text, fallbackMenu) {
    if (typeof fetch !== 'function' || typeof ReadableStream === 'undefined') return false;

    let res;
    try {
      res = await fetch('/assistant/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: aiChatHistory, lang: state.lang, conversation_id: state.conversationId }),
      });
    } catch (e) {
      return false; // network-level failure — let the non-streaming fallback try
    }
    if (!res.ok || !res.body) return false;

    const typing = showTyping();
    let bubble = null;      // created lazily on first token, replaces the typing indicator
    let statusBubble = null; // shows "checking stock..." etc. while a tool runs
    let fullText = '';
    let sawAnyEvent = false;
    let sawWidget = false;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function ensureBubble() {
      if (!bubble) {
        typing.remove();
        if (statusBubble) { statusBubble.remove(); statusBubble = null; }
        bubble = addBotMessage('', { speak: false });
      }
      return bubble;
    }

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line.
        let frameEnd;
        while ((frameEnd = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, frameEnd);
          buffer = buffer.slice(frameEnd + 2);
          const eventLine = frame.split('\n').find((l) => l.startsWith('event: '));
          const dataLine = frame.split('\n').find((l) => l.startsWith('data: '));
          if (!eventLine || !dataLine) continue;
          const eventName = eventLine.slice('event: '.length).trim();
          let payload;
          try { payload = JSON.parse(dataLine.slice('data: '.length)); } catch (e) { continue; }
          sawAnyEvent = true;

          if (eventName === 'token') {
            fullText += payload.text || '';
            const div = ensureBubble();
            div.innerHTML = escapeHtml(fullText).replace(/\n/g, '<br>');
            scrollToBottom();
          } else if (eventName === 'status') {
            if (!bubble) {
              if (statusBubble) statusBubble.remove();
              statusBubble = addBotMessage(`<span class="assistant-status-hint">${escapeHtml(toolStatusLabel(payload.tool))}</span>`, { speak: false });
              statusBubble.classList.add('assistant-status-msg');
            }
          } else if (eventName === 'widget') {
            sawWidget = true;
            if (!bubble && statusBubble) { statusBubble.remove(); statusBubble = null; }
            typing.remove();
            renderWidget(payload);
          } else if (eventName === 'done') {
            typing.remove();
            if (statusBubble) { statusBubble.remove(); statusBubble = null; }
            const finalText = payload.reply || fullText || '...';
            const bubbleAlreadyExisted = !!bubble;
            if (!bubble) {
              bubble = addBotMessage(escapeHtml(finalText).replace(/\n/g, '<br>')); // speaks by default if voice input
            } else if (finalText !== fullText) {
              bubble.innerHTML = escapeHtml(finalText).replace(/\n/g, '<br>');
              persistState();
            } else {
              persistState();
            }
            // The streamed-token bubble was created with speak:false (it
            // was empty at the time), so speak it now that it's complete.
            if (bubbleAlreadyExisted && state.lastInputWasVoice) speak(bubble.textContent);
            (payload.widgets || []).forEach((w) => { if (!sawWidget) renderWidget(w); });

            aiChatHistory.push({ role: 'user', content: text });
            aiChatHistory.push({ role: 'assistant', content: finalText });
            if (aiChatHistory.length > 20) aiChatHistory = aiChatHistory.slice(-20);
          } else if (eventName === 'error') {
            typing.remove();
            if (statusBubble) { statusBubble.remove(); statusBubble = null; }
            addBotMessage(payload.message || t('connection_error'));
            fallbackMenu();
          }
        }
      }
    } catch (e) {
      typing.remove();
      if (statusBubble) statusBubble.remove();
      if (!sawAnyEvent) return false; // nothing rendered yet — safe to let the fallback retry cleanly
      // Partial stream then a hard failure — don't leave the person hanging.
      addBotMessage(t('connection_error'));
      fallbackMenu();
    }

    return sawAnyEvent;
  }

  async function handlePaySalaryIntent(employee, amount) {
    const salF = I18N[state.lang].salary.form;
    const confirmed = await confirmAction(
      buildReviewCard(t('salary.add'), [
        [salF.employee, escapeHtml(employee)],
        [salF.amount, fmtMoney(amount)],
      ])
    );
    if (!confirmed) { addBotMessage(t('confirm_cancelled')); addOptions([catBack(showSalaryMenu)]); return; }
    const res = await apiCall('/assistant/api/salary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ employee, amount }),
    });
    if (res.ok && res.data.success) {
      addBotMessage(t('salary.paid')(escapeHtml(employee), fmtMoney(amount)));
    } else {
      addBotMessage((res.data && res.data.message) || res.message || t('generic_error'));
    }
    addOptions([catBack(showSalaryMenu)]);
  }

  inputForm?.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textInput.value;
    if (!text.trim()) return;
    if (!opened) { opened = true; showMainMenu(true); }
    addUserMessage(text);
    textInput.value = '';
    processUserText(text);
  });

  // ---------------------------------------------------------------------
  // Voice input: MediaRecorder + server-side transcription (like ChatGPT),
  // falling back to the browser's Web Speech API only on very old browsers
  // that don't support getUserMedia/MediaRecorder at all.
  //
  // Why the switch: the previous version relied only on the Web Speech API
  // (webkitSpeechRecognition), which is Chrome/Chromium-only, has no
  // Firefox implementation, is inconsistent on Safari, and silently
  // streams audio to Google's own speech servers -- so it can fail in ways
  // that have nothing to do with this app's own OPENAI_API_KEY. Recording
  // locally with MediaRecorder and transcribing on our own server (Whisper
  // via /assistant/api/transcribe) behaves the same on every modern
  // browser/device and only depends on this app's own configuration.
  // ---------------------------------------------------------------------
  const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

  let mediaRecorder = null;
  let recordedChunks = [];
  let recordStream = null;

  async function startRecording() {
    try {
      recordStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (e) {
      if (!opened) { opened = true; showMainMenu(true); }
      addBotMessage(t('mic_permission_denied'), { speak: false });
      return;
    }

    // Everything past this point (codec selection, MediaRecorder itself,
    // .start()) can also throw on some browsers/devices — previously only
    // getUserMedia was guarded, so a failure here left the mic stream open
    // with no recorder running and no message shown: the button just did
    // nothing, with no way to tell why.
    try {
      recordedChunks = [];
      const supported = ['audio/webm', 'audio/mp4', 'audio/ogg'].filter((m) => window.MediaRecorder.isTypeSupported && window.MediaRecorder.isTypeSupported(m));
      const mimeType = supported.length ? supported[0] : '';
      mediaRecorder = mimeType ? new MediaRecorder(recordStream, { mimeType: mimeType }) : new MediaRecorder(recordStream);
      mediaRecorder.addEventListener('dataavailable', function (e) { if (e.data && e.data.size > 0) recordedChunks.push(e.data); });
      mediaRecorder.addEventListener('stop', onRecordingStop);
      mediaRecorder.start();
    } catch (e) {
      if (recordStream) recordStream.getTracks().forEach((tr) => tr.stop());
      recordStream = null;
      mediaRecorder = null;
      if (!opened) { opened = true; showMainMenu(true); }
      addBotMessage(t('mic_error'), { speak: false });
      return;
    }

    recognizing = true;
    micBtn.classList.add('listening');
    if (textInput) {
      textInput.classList.add('listening-preview');
      textInput.placeholder = t('listening_placeholder');
    }
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
    if (recordStream) recordStream.getTracks().forEach(function (tr) { tr.stop(); });
    recognizing = false;
    micBtn.classList.remove('listening');
    micBtn.classList.add('transcribing');
    if (textInput) {
      textInput.classList.remove('listening-preview');
      textInput.placeholder = t('input_placeholder');
    }
  }

  async function onRecordingStop() {
    micBtn.classList.remove('transcribing');
    if (!recordedChunks.length) {
      if (!opened) { opened = true; showMainMenu(true); }
      addBotMessage(t('mic_too_short'), { speak: false });
      return;
    }
    const blobType = (mediaRecorder && mediaRecorder.mimeType) || 'audio/webm';
    const blob = new Blob(recordedChunks, { type: blobType });
    if (blob.size < 800) {
      if (!opened) { opened = true; showMainMenu(true); }
      addBotMessage(t('mic_too_short'), { speak: false });
      return;
    }

    const extPart = blobType.split('/')[1] || 'webm';
    const ext = extPart.split(';')[0];
    const formData = new FormData();
    formData.append('audio', blob, 'voice.' + ext);
    // Only pass a language hint when the person deliberately chose one via
    // the flag chips. A forced-but-wrong hint makes Whisper mis-transcribe
    // (it tries to force-fit the audio into that language instead of
    // rejecting it), which is worse than just letting it auto-detect.
    if (state.forceLangForVoice && SUPPORTED_LANGS.includes(state.lang)) {
      formData.append('lang', state.lang);
    }

    try {
      const res = await fetch('/assistant/api/transcribe', { method: 'POST', body: formData });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok || !data.success) {
        if (!opened) { opened = true; showMainMenu(true); }
        addBotMessage((data && data.message) || t('mic_error'), { speak: false });
        return;
      }
      const text = (data.text || '').trim();
      if (!text) {
        if (!opened) { opened = true; showMainMenu(true); }
        addBotMessage(t('mic_too_short'), { speak: false });
        return;
      }
      // Sync the UI to whichever language Whisper actually detected in the
      // audio (unless the person forced one), same as typed-text detection.
      if (!state.forceLangForVoice) {
        const detectedFromAudio = SUPPORTED_LANGS.includes(data.detected_lang) ? data.detected_lang : detectLanguage(text);
        if (detectedFromAudio) applyLanguage(detectedFromAudio);
      }
      if (!opened) { opened = true; showMainMenu(true); }
      state.lastInputWasVoice = true;
      addUserMessage(text);
      processUserText(text);
    } catch (e) {
      if (!opened) { opened = true; showMainMenu(true); }
      addBotMessage(t('mic_error'), { speak: false });
    }
  }

  if (canRecord) {
    if (micBtn) micBtn.addEventListener('click', function () {
      if (recognizing) { stopRecording(); return; }
      startRecording();
    });
  } else if (SpeechRecognitionCtor) {
    recognition = new SpeechRecognitionCtor();
    // continuous + interimResults gives live, word-by-word feedback in the
    // input field while the person is still talking (like modern voice
    // assistants), instead of the mic just sitting there silently until
    // they stop — that silent wait is what reads as slow/dated. We still
    // auto-stop and send as soon as we get a final chunk, so the one-shot
    // "tap, say a sentence, done" flow is unchanged.
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.lang = SPEECH_LANG[state.lang];

    recognition.addEventListener('start', () => {
      recognizing = true;
      micBtn.classList.add('listening');
      if (textInput) {
        textInput.value = '';
        textInput.classList.add('listening-preview');
        textInput.placeholder = t('listening_placeholder');
      }
    });
    recognition.addEventListener('end', () => {
      recognizing = false;
      micBtn.classList.remove('listening');
      if (textInput) {
        textInput.classList.remove('listening-preview');
        textInput.placeholder = t('input_placeholder');
      }
    });
    recognition.addEventListener('error', (event) => {
      recognizing = false;
      micBtn.classList.remove('listening');
      // 'no-speech' / 'aborted' happen on routine timeouts or the user
      // stopping it themselves — not worth interrupting the chat for.
      if (event.error === 'no-speech' || event.error === 'aborted') return;
      if (!opened) { opened = true; showMainMenu(true); }
      if (event.error === 'not-allowed' || event.error === 'permission-denied' || event.error === 'service-not-allowed') {
        addBotMessage(t('mic_permission_denied'), { speak: false });
      } else {
        addBotMessage(t('mic_error'), { speak: false });
      }
    });
    recognition.addEventListener('result', (event) => {
      let finalTranscript = '';
      let interimTranscript = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const chunk = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalTranscript += chunk;
        else interimTranscript += chunk;
      }
      // Live preview so the person sees it's actually listening, in real
      // time, rather than wondering if the mic caught anything.
      if (interimTranscript && textInput) textInput.value = interimTranscript;
      if (finalTranscript) {
        recognition.stop();
        if (textInput) textInput.value = '';
        if (!opened) { opened = true; showMainMenu(true); }
        state.lastInputWasVoice = true;
        const text = finalTranscript.trim();
        addUserMessage(text);
        processUserText(text);
      }
    });

    if (micBtn) micBtn.addEventListener('click', () => {
      if (recognizing) { recognition.stop(); return; }
      try {
        recognition.lang = SPEECH_LANG[state.lang];
        recognition.start();
      } catch (e) { /* already started */ }
    });
  } else {
    if (micBtn) {
      micBtn.classList.add('unsupported');
      micBtn.setAttribute('title', 'Voice input not supported in this browser');
    }
  }

  // Cache the browser's available voices (they load asynchronously in
  // some browsers) and pick the best-sounding one for the current
  // language, preferring higher-quality "Natural/Neural/Enhanced/Google"
  // voices over the default robotic system voice.
  let cachedVoices = [];
  function refreshVoices() {
    if ('speechSynthesis' in window) cachedVoices = window.speechSynthesis.getVoices() || [];
  }
  if ('speechSynthesis' in window) {
    refreshVoices();
    window.speechSynthesis.addEventListener('voiceschanged', refreshVoices);
  }

  function pickVoice(langCode) {
    if (!cachedVoices.length) refreshVoices();
    const base = langCode.split('-')[0];
    const candidates = cachedVoices.filter((v) => v.lang && v.lang.toLowerCase().startsWith(base));
    if (!candidates.length) return null;
    const qualityRe = /natural|neural|enhanced|premium|google|online/i;
    const best = candidates.find((v) => qualityRe.test(v.name)) || candidates[0];
    return best;
  }

  function speak(text) {
    if (!('speechSynthesis' in window) || !text) return;
    try {
      const utter = new SpeechSynthesisUtterance(text);
      const langCode = SPEECH_LANG[state.lang];
      utter.lang = langCode;
      const voice = pickVoice(langCode);
      if (voice) utter.voice = voice;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utter);
    } catch (e) { /* ignore */ }
  }

  // Resume the conversation (if any) now that every menu function and
  // CATEGORY_ENTRY above has been defined.
  restoreState();
})();