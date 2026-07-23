/**
 * Reusable Stripe Terminal (physical card reader) payment flow.
 *
 * Shared by the Kasse "Verkaufen" (sell) screen and the Schulden (debts)
 * card-payment popups — both just need "take €X by card at the register"
 * and don't need to know anything about Stripe's SDK internals.
 *
 * Requires, in this order, on any page that uses it:
 *   1. <script src="https://js.stripe.com/terminal/v1/"></script>
 *   2. window.STRIPE_TERMINAL_CONFIG = {{ stripe_terminal | tojson }};
 *      (injected server-side — see the `stripe_terminal` context processor
 *      in app.py: { configured, location_id, simulated })
 *   3. <script src="{{ url_for('static', filename='js/stripe-terminal.js') }}"></script>
 *
 * Usage:
 *   StripeTerminalPay.collectPayment(12.50, { context: 'sale', reference: saleId }, {
 *     onStatus: (statusKey) => { ... update some UI text ... },
 *   }).then((paymentIntentId) => {
 *     // put paymentIntentId into a hidden form field / request body and
 *     // submit — the server re-verifies it with Stripe before trusting it.
 *   }).catch((err) => {
 *     // show err.message to the cashier; they can retry.
 *   });
 *
 * Status keys passed to onStatus (map these to translated text yourself):
 *   'reader_discovering' | 'reader_connecting' | 'creating_intent' |
 *   'present_card' | 'processing'
 */
if (!window.StripeTerminalPay) {
const StripeTerminalPay = (function () {
  let terminal = null;
  let readerConnected = false;

  function getConfig() {
    return window.STRIPE_TERMINAL_CONFIG || { configured: false, location_id: '', simulated: true };
  }

  async function fetchConnectionToken() {
    const res = await fetch('/stripe/connection_token', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.secret) {
      throw new Error(data.error || 'Could not get a Stripe connection token.');
    }
    return data.secret;
  }

  function getTerminal() {
    if (!window.StripeTerminal) {
      throw new Error('Stripe Terminal SDK did not load (check your internet connection).');
    }
    if (!terminal) {
      terminal = window.StripeTerminal.create({
        onFetchConnectionToken: fetchConnectionToken,
        onUnexpectedReaderDisconnect: function () { readerConnected = false; },
      });
    }
    return terminal;
  }

  async function ensureReaderConnected(onStatus) {
    const config = getConfig();
    if (!config.configured) {
      throw new Error('Stripe is not configured on the server yet.');
    }
    const t = getTerminal();
    if (readerConnected) return;

    onStatus && onStatus('reader_discovering');
    const discoverConfig = config.simulated
      ? { simulated: true }
      : { location: config.location_id || undefined };
    const discoverResult = await t.discoverReaders(discoverConfig);
    if (discoverResult.error) {
      throw new Error(discoverResult.error.message || 'Could not discover a card reader.');
    }
    const readers = discoverResult.discoveredReaders;
    if (!readers || !readers.length) {
      throw new Error('No card reader found. Make sure it is powered on and reachable.');
    }

    onStatus && onStatus('reader_connecting');
    const connectResult = await t.connectReader(readers[0]);
    if (connectResult.error) {
      throw new Error(connectResult.error.message || 'Could not connect to the card reader.');
    }
    readerConnected = true;
  }

  /**
   * @param {number} amountEur - amount in euros (e.g. 12.5), not cents.
   * @param {{context: string, reference: string}} meta - what this payment
   *   is for, purely for the Stripe Dashboard (e.g. context: 'sale',
   *   reference: saleId, or context: 'debt_payment', reference: debtId).
   * @param {{onStatus?: (key: string) => void}} hooks
   * @returns {Promise<string>} the succeeded PaymentIntent id.
   */
  async function collectPayment(amountEur, meta, hooks) {
    hooks = hooks || {};
    const onStatus = hooks.onStatus || function () {};
    meta = meta || {};

    await ensureReaderConnected(onStatus);
    const t = getTerminal();

    onStatus('creating_intent');
    const piRes = await fetch('/stripe/create_payment_intent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: amountEur, context: meta.context, reference: meta.reference }),
    });
    const piData = await piRes.json().catch(() => ({}));
    if (!piRes.ok || !piData.client_secret) {
      throw new Error(piData.error || 'Could not start the card payment.');
    }

    onStatus('present_card');
    const collectResult = await t.collectPaymentMethod(piData.client_secret);
    if (collectResult.error) {
      throw new Error(collectResult.error.message || 'Card payment was cancelled or failed.');
    }

    onStatus('processing');
    const processResult = await t.processPayment(collectResult.paymentIntent);
    if (processResult.error) {
      throw new Error(processResult.error.message || 'Card payment could not be processed.');
    }

    const intent = processResult.paymentIntent;
    if (intent.status !== 'succeeded' && intent.status !== 'requires_capture') {
      throw new Error('Unexpected payment status: ' + intent.status);
    }
    return intent.id;
  }

  return { collectPayment, ensureReaderConnected };
})();

window.StripeTerminalPay = StripeTerminalPay;
}
