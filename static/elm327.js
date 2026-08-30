/*
 * ELM327 adapter, driven by the browser (SPEC §8.3c).
 *
 * The server runs in a container with no USB and no Bluetooth, and the device
 * actually near the car is the phone in your hand — so this file talks to the
 * adapter and posts what it read. Read-only apart from one clearing command,
 * which asks a question naming the vehicle first.
 *
 * Requires a secure context and a Chromium browser. Both are stated on the
 * page rather than discovered as a grayed-out button.
 */
(function () {
  "use strict";

  var strings = JSON.parse(document.getElementById("elm-strings").textContent);
  var config = JSON.parse(document.getElementById("elm-config").textContent);

  var log = document.getElementById("elm-log");
  var status = document.getElementById("elm-status");
  var results = document.getElementById("elm-results");
  var saveButton = document.getElementById("elm-save");
  var clearButton = document.getElementById("elm-clear");

  var port = null;
  var reader = null;
  var writer = null;
  var found = [];

  function say(message, kind) {
    var line = document.createElement("div");
    line.className = "small " + (kind || "muted");
    line.textContent = message;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function setStatus(message) {
    status.textContent = message;
  }

  // -- transport ----------------------------------------------------------

  async function connect() {
    if (!("serial" in navigator)) {
      setStatus(strings.noSerial);
      return false;
    }
    port = await navigator.serial.requestPort();
    await port.open({ baudRate: config.baudRate });
    var decoder = new TextDecoderStream();
    port.readable.pipeTo(decoder.writable);
    reader = decoder.readable.getReader();
    var encoder = new TextEncoderStream();
    encoder.readable.pipeTo(port.writable);
    writer = encoder.writable.getWriter();
    return true;
  }

  async function disconnect() {
    try {
      if (reader) { await reader.cancel(); }
      if (writer) { await writer.close(); }
      if (port) { await port.close(); }
    } catch (err) {
      // Closing a port the adapter already dropped throws, and there is
      // nothing useful to do about it.
    }
    port = reader = writer = null;
  }

  /*
   * Send one AT or OBD command and collect until the '>' prompt.
   *
   * The prompt is the only reliable end-of-response marker: an ELM327 streams
   * partial lines and a fixed delay either truncates a long reply or wastes a
   * second on every short one.
   */
  async function send(command, timeoutMs) {
    await writer.write(command + "\r");
    var buffer = "";
    var deadline = Date.now() + (timeoutMs || 5000);
    while (Date.now() < deadline) {
      var chunk = await Promise.race([
        reader.read(),
        new Promise(function (resolve) {
          setTimeout(function () { resolve({ value: "", done: false }); }, 250);
        })
      ]);
      if (chunk.done) { break; }
      buffer += chunk.value || "";
      if (buffer.indexOf(">") >= 0) { break; }
    }
    return buffer.replace(/>/g, "").trim();
  }

  // -- decoding -----------------------------------------------------------

  var SYSTEMS = ["P", "C", "B", "U"];

  /*
   * Two bytes become one code. The top two bits pick the system letter and the
   * next two the leading digit, which is why `0133` is P0133 and `4133` is
   * C0133 — the same three nibbles under a different heading.
   */
  function decodePair(high, low) {
    if (high === 0 && low === 0) { return null; }
    var letter = SYSTEMS[(high >> 6) & 0x03];
    var first = (high >> 4) & 0x03;
    var rest = ((high & 0x0f).toString(16) + (low < 16 ? "0" : "") + low.toString(16));
    return (letter + first + rest).toUpperCase();
  }

  /*
   * Pull codes out of a mode 03/07/0A response.
   *
   * Every line is handled independently because a multi-frame reply arrives as
   * numbered lines ("0: 43 04 01 33 …"), and the ISO-TP frame counter would
   * otherwise be decoded as half of a trouble code.
   */
  function decodeResponse(text, replyByte) {
    var codes = [];
    text.split(/[\r\n]+/).forEach(function (line) {
      var cleaned = line.replace(/^\s*\d+\s*:/, "").replace(/[^0-9A-Fa-f ]/g, " ").trim();
      if (!cleaned) { return; }
      var bytes = cleaned.split(/\s+/).map(function (b) { return parseInt(b, 16); })
        .filter(function (b) { return !isNaN(b); });
      var start = bytes.indexOf(replyByte);
      if (start < 0) { return; }
      // The byte after the mode reply is a count on some adapters and the first
      // code's high byte on others. Pairing from the count byte when it is one
      // would shift every code by a nibble, so odd leftovers are dropped rather
      // than guessed at.
      var payload = bytes.slice(start + 1);
      for (var i = 0; i + 1 < payload.length; i += 2) {
        var code = decodePair(payload[i], payload[i + 1]);
        if (code) { codes.push(code); }
      }
    });
    return codes;
  }

  function record(code, state) {
    if (found.some(function (c) { return c.code === code && c.state === state; })) { return; }
    found.push({ code: code, state: state });
  }

  function render() {
    results.innerHTML = "";
    if (!found.length) {
      results.textContent = strings.noCodes;
      saveButton.disabled = true;
      return;
    }
    found.forEach(function (entry) {
      var row = document.createElement("div");
      row.className = "row";
      row.innerHTML = '<span class="mono">' + entry.code + "</span>";
      var state = document.createElement("span");
      state.className = "small muted";
      state.textContent = entry.state;
      row.appendChild(state);
      results.appendChild(row);
    });
    saveButton.disabled = false;
  }

  // -- the read -----------------------------------------------------------

  async function read() {
    found = [];
    render();
    try {
      if (!(await connect())) { return; }
    } catch (err) {
      setStatus(strings.notConnected);
      say(String(err), "warn");
      return;
    }

    try {
      setStatus(strings.connected);
      await send("ATZ", 6000);
      await send("ATE0");       // no echo, so replies are not doubled
      await send("ATL0");
      await send("ATS0");       // no spaces would be smaller; spaces are easier to read back
      await send("ATSP0");      // let the adapter negotiate the protocol

      var modes = [["03", 0x43, strings.stored], ["07", 0x47, strings.pending],
                   ["0A", 0x4A, strings.permanent]];
      for (var i = 0; i < modes.length; i++) {
        var mode = modes[i];
        var reply = await send(mode[0], 8000);
        say(mode[2] + ": " + reply.replace(/[\r\n]+/g, " "));
        decodeResponse(reply, mode[1]).forEach(function (code) {
          record(code, mode[2]);
        });
      }
      render();
      setStatus(found.length ? strings.done : strings.noCodes);
    } catch (err) {
      setStatus(strings.readFailed);
      say(String(err), "warn");
    } finally {
      await disconnect();
    }
  }

  async function save() {
    saveButton.disabled = true;
    var response = await fetch(config.captureUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": config.csrf },
      body: JSON.stringify({ adapter: "ELM327", codes: found })
    });
    if (!response.ok) {
      setStatus(strings.saveFailed);
      saveButton.disabled = false;
      return;
    }
    var data = await response.json();
    window.location = data.url;
  }

  async function clearCodes() {
    // Deliberately a full confirm() naming the vehicle. Clearing before an
    // emissions test resets the readiness monitors and costs a re-test, and it
    // is not a mistake a toast can undo.
    if (!window.confirm(strings.clearWarning)) { return; }
    try {
      if (!(await connect())) { return; }
      await send("ATZ", 6000);
      await send("ATE0");
      await send("ATSP0");
      var reply = await send("04", 8000);
      say("04: " + reply.replace(/[\r\n]+/g, " "));
      setStatus(strings.cleared);
    } catch (err) {
      setStatus(strings.readFailed);
      say(String(err), "warn");
    } finally {
      await disconnect();
    }
  }

  document.getElementById("elm-read").addEventListener("click", read);
  saveButton.addEventListener("click", save);
  if (clearButton) { clearButton.addEventListener("click", clearCodes); }

  if (!window.isSecureContext) {
    setStatus(strings.insecure);
  } else if (!("serial" in navigator)) {
    setStatus(strings.noSerial);
  } else {
    setStatus(strings.ready);
  }
})();
