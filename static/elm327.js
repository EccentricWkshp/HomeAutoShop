/*
 * ELM327 adapter, driven by the browser (SPEC §8.3c).
 *
 * The server runs in a container with no USB and no Bluetooth, and the device
 * actually near the car is the phone in your hand — so this file talks to the
 * adapter and posts what it read. Read-only apart from one clearing command,
 * which asks a question naming the vehicle first.
 *
 * Two transports, because no single one reaches every adapter:
 *
 *   Web Serial     wired adapters, and Bluetooth Classic (RFCOMM/SPP) ones.
 *   Web Bluetooth  BLE adapters, which RFCOMM cannot see at all.
 *
 * The Bluetooth half of Web Serial has a trap worth stating here, because it
 * looks exactly like a broken adapter. `requestPort()` shows an *unmapped*
 * Bluetooth port only when it carries the standard SerialPort service class
 * (0x1101); anything using a vendor UUID is withheld unless the page names
 * that UUID in `allowedBluetoothServiceClassIds` — whether or not it filters.
 * Desktops hide this, because the OS maps a paired adapter to a COM port or a
 * device node and mapped ports are always listed. Android maps nothing, so the
 * same paired adapter that works on a laptop yields "No compatible devices
 * found" on a phone. Hence the allow-list, and hence `describePort` printing
 * the service class ID of whatever does connect: the UUID a vendor never
 * documents is discoverable by connecting once on a desktop and reading it.
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
  var transportChoice = document.getElementById("elm-transport");

  var transport = null;
  var incoming = "";
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

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function chosenTransport() {
    return transportChoice ? transportChoice.value : "serial";
  }

  // -- Web Serial ---------------------------------------------------------

  /*
   * Print what we actually connected to.
   *
   * For a Bluetooth port this is the only way to learn the vendor's service
   * class UUID — it is not in the adapter's documentation and not visible in
   * the chooser. Read it off a desktop once, add it to the allow-list, and the
   * same adapter becomes selectable on a phone.
   */
  function describePort(port) {
    if (!port.getInfo) { return; }
    var info = port.getInfo() || {};
    if (info.bluetoothServiceClassId) {
      say(strings.serviceClassId + " " + info.bluetoothServiceClassId);
    } else if (info.usbVendorId !== undefined && info.usbVendorId !== null) {
      say(strings.usbIds + " " + info.usbVendorId + ":" + info.usbProductId);
    }
  }

  async function openSerial() {
    if (!("serial" in navigator)) { throw new Error(strings.noSerial); }

    var options = {};
    if (config.bluetoothServiceUuids && config.bluetoothServiceUuids.length) {
      options.allowedBluetoothServiceClassIds = config.bluetoothServiceUuids;
    }

    var port;
    try {
      port = await navigator.serial.requestPort(options);
    } catch (err) {
      // Chrome rejects the whole call if it dislikes one UUID in the list, so
      // a bad entry in the allow-list would otherwise look like a missing
      // adapter. Retrying bare at least reaches wired and standard-SPP ports.
      if (err && err.name === "TypeError" && options.allowedBluetoothServiceClassIds) {
        say(strings.badUuidList, "warn");
        port = await navigator.serial.requestPort();
      } else {
        throw err;
      }
    }

    await port.open({ baudRate: config.baudRate });
    describePort(port);

    var decoder = new TextDecoderStream();
    port.readable.pipeTo(decoder.writable).catch(function () { });
    var reader = decoder.readable.getReader();
    var encoder = new TextEncoderStream();
    encoder.readable.pipeTo(port.writable).catch(function () { });
    var writer = encoder.writable.getWriter();

    // Pump into the shared buffer rather than reading inside send(). An ELM327
    // streams partial lines, and a read that is only running while a command
    // is outstanding drops whatever arrives between them.
    (async function pump() {
      try {
        for (;;) {
          var chunk = await reader.read();
          if (chunk.done) { return; }
          incoming += chunk.value || "";
        }
      } catch (err) {
        // The port closed underneath us, which is how disconnect() ends.
      }
    })();

    return {
      write: function (text) { return writer.write(text); },
      close: async function () {
        try { await reader.cancel(); } catch (err) { /* already gone */ }
        try { await writer.close(); } catch (err) { /* already gone */ }
        try { await port.close(); } catch (err) { /* already gone */ }
      }
    };
  }

  // -- Web Bluetooth (BLE) ------------------------------------------------

  /*
   * BLE adapters expose a serial pipe as a pair of characteristics rather than
   * a port: one to write commands to, one that notifies with the reply. The
   * pairing differs per vendor, so each candidate is tried in turn and the
   * first that resolves wins.
   */
  async function openBluetooth() {
    if (!("bluetooth" in navigator)) { throw new Error(strings.noBluetooth); }

    var profiles = config.bleProfiles || [];
    if (!profiles.length) { throw new Error(strings.noBleProfile); }

    var services = profiles.map(function (profile) { return profile.service; });
    // acceptAllDevices rather than a service filter: plenty of adapters never
    // advertise their service UUID, so filtering on it hides the very devices
    // we can talk to. The services still have to be declared to be reachable.
    var device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,
      optionalServices: services
    });
    say(strings.chosenDevice + " " + (device.name || device.id));

    var server = await device.gatt.connect();
    var decoder = new TextDecoder();

    for (var i = 0; i < profiles.length; i++) {
      var profile = profiles[i];
      try {
        var service = await server.getPrimaryService(profile.service);
        var notifyChar = await service.getCharacteristic(profile.notify);
        var writeChar = profile.write === profile.notify
          ? notifyChar
          : await service.getCharacteristic(profile.write);

        await notifyChar.startNotifications();
        notifyChar.addEventListener("characteristicvaluechanged", function (event) {
          incoming += decoder.decode(event.target.value);
        });
        say(strings.usingProfile + " " + profile.service);

        return {
          write: function (text) { return writeChunks(writeChar, text); },
          close: async function () {
            try { await notifyChar.stopNotifications(); } catch (err) { /* gone */ }
            try { device.gatt.disconnect(); } catch (err) { /* gone */ }
          }
        };
      } catch (err) {
        // Wrong profile for this adapter; try the next one.
      }
    }

    try { device.gatt.disconnect(); } catch (err) { /* gone */ }
    throw new Error(strings.noBleProfile);
  }

  /*
   * Split a command across writes.
   *
   * Android negotiates a 23-byte MTU by default, leaving 20 bytes of payload,
   * and a longer write is rejected outright rather than fragmented. Commands
   * are short enough that this almost never splits — but "almost never" is the
   * kind of bug that only shows up on somebody else's phone.
   */
  async function writeChunks(characteristic, text) {
    var bytes = new TextEncoder().encode(text);
    var size = config.bleChunkBytes || 20;
    for (var i = 0; i < bytes.length; i += size) {
      var slice = bytes.slice(i, i + size);
      if (characteristic.writeValueWithoutResponse) {
        await characteristic.writeValueWithoutResponse(slice);
      } else {
        await characteristic.writeValue(slice);
      }
    }
  }

  // -- transport ----------------------------------------------------------

  async function connect() {
    incoming = "";
    transport = chosenTransport() === "bluetooth"
      ? await openBluetooth()
      : await openSerial();
    return true;
  }

  async function disconnect() {
    if (transport) {
      try {
        await transport.close();
      } catch (err) {
        // Closing something the adapter already dropped throws, and there is
        // nothing useful to do about it.
      }
    }
    transport = null;
  }

  /*
   * Send one AT or OBD command and collect until the '>' prompt.
   *
   * The prompt is the only reliable end-of-response marker: an ELM327 streams
   * partial lines and a fixed delay either truncates a long reply or wastes a
   * second on every short one.
   */
  async function send(command, timeoutMs) {
    incoming = "";
    await transport.write(command + "\r");
    var deadline = Date.now() + (timeoutMs || 5000);
    while (Date.now() < deadline) {
      if (incoming.indexOf(">") >= 0) { break; }
      await sleep(40);
    }
    return incoming.replace(/>/g, "").trim();
  }

  // -- decoding -----------------------------------------------------------

  var SYSTEMS = ["P", "C", "B", "U"];

  /*
   * Replies that mean "the read did not happen".
   *
   * These come from the adapter, not the car, and they decode to zero codes —
   * which is indistinguishable from a healthy vehicle unless we look. Saying
   * "no codes found" to somebody whose adapter never reached an ECU is the
   * worst answer this page could give: it is the one that ends the diagnosis.
   *
   * NO DATA is deliberately absent. It means the ECU declined that one mode,
   * which is ordinary for pending and permanent codes on plenty of cars.
   */
  var LINK_ERROR = /UNABLE TO CONNECT|BUS INIT|BUS ERROR|BUS BUSY|CAN ERROR|DATA ERROR|FB ERROR|LV RESET|STOPPED/i;

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
      // A token is one byte when the adapter prints spaces and the entire
      // frame when it does not, so anything longer than a pair is cut back
      // into pairs. Asking for spaces (ATS1) is not enough on its own: an
      // adapter that was left in ATS0 by another app, or a clone that ignores
      // the command, would otherwise parse "430133" as a single huge number
      // and report a car with codes as a car with none.
      var bytes = [];
      cleaned.split(/\s+/).forEach(function (token) {
        if (token.length <= 2) {
          var single = parseInt(token, 16);
          if (!isNaN(single)) { bytes.push(single); }
          return;
        }
        for (var at = 0; at + 2 <= token.length; at += 2) {
          var value = parseInt(token.substr(at, 2), 16);
          if (!isNaN(value)) { bytes.push(value); }
        }
      });
      var start = bytes.indexOf(replyByte);
      if (start < 0) { return; }
      var payload = bytes.slice(start + 1);
      /*
       * Drop the DTC count byte when there is one, and parity says when.
       *
       * CAN (ISO 15765-4, which is every car since 2008) answers mode 03 with
       * `43 <count> <hi lo> …`; the older serial protocols answer `43 <hi lo> …`
       * with no count at all. Counted payloads are therefore always odd —
       * 1 + 2n — and uncounted ones always even, including the zero-padding a
       * short CAN frame carries, since that pads in whole byte pairs.
       *
       * Getting this wrong is worse than reading nothing: pairing from the
       * count byte shifts every code by a byte, so `43 02 01 33 04 20` reads
       * as P0201 instead of P0133 and P0420 — a real-looking code for a fault
       * the car does not have, which is a morning spent chasing the wrong
       * circuit.
       */
      if (payload.length % 2 === 1) { payload = payload.slice(1); }
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
      var code = document.createElement("span");
      code.className = "mono";
      code.textContent = entry.code;
      row.appendChild(code);
      var state = document.createElement("span");
      state.className = "small muted";
      state.textContent = entry.state;
      row.appendChild(state);
      results.appendChild(row);
    });
    saveButton.disabled = false;
  }

  /*
   * Explain a failure to choose an adapter.
   *
   * The browser reports the same NotFoundError whether somebody pressed Cancel
   * or the chooser had nothing in it to press — so the raw message ("No port
   * selected by the user") reads as a decision the user made, when the usual
   * cause is an empty list. Say both.
   */
  function explainNoChoice(err) {
    if (err && err.name === "NotFoundError") {
      setStatus(strings.nothingChosen);
      say(strings.nothingChosenHelp, "warn");
      return true;
    }
    return false;
  }

  // -- the read -----------------------------------------------------------

  async function read() {
    found = [];
    render();
    try {
      await connect();
    } catch (err) {
      if (!explainNoChoice(err)) {
        setStatus(strings.notConnected);
        say(String(err), "warn");
      }
      return;
    }

    try {
      setStatus(strings.connected);
      await send("ATZ", 6000);
      await send("ATE0");       // no echo, so replies are not doubled
      await send("ATL0");
      // ATS1, not ATS0. S0 turns spaces *off*, which packs a reply into one
      // run of hex ("430133") — and the decoder below splits on whitespace to
      // find byte boundaries, so every code silently decoded to nothing. The
      // bytes are the same either way; the separators are what make them
      // readable, both to this code and to anyone reading the log.
      await send("ATS1");
      await send("ATSP0");      // let the adapter negotiate the protocol

      var modes = [["03", 0x43, strings.stored], ["07", 0x47, strings.pending],
                   ["0A", 0x4A, strings.permanent]];
      var reached = 0;
      for (var i = 0; i < modes.length; i++) {
        var mode = modes[i];
        var reply = await send(mode[0], 8000);
        var failed = LINK_ERROR.test(reply);
        say(mode[2] + ": " + reply.replace(/[\r\n]+/g, " "), failed ? "warn" : "muted");
        if (!failed) { reached += 1; }
        decodeResponse(reply, mode[1]).forEach(function (code) {
          record(code, mode[2]);
        });
      }
      render();

      // Nothing answered, so there is nothing to say about this car yet.
      if (!reached) {
        setStatus(strings.noEcu);
        say(strings.noEcuHelp, "warn");
        return;
      }
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
      await connect();
    } catch (err) {
      if (!explainNoChoice(err)) {
        setStatus(strings.notConnected);
        say(String(err), "warn");
      }
      return;
    }
    try {
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

  /*
   * State what this browser can do before anything is pressed.
   *
   * Neither transport is universal — Web Serial reaches wired and Classic
   * adapters, Web Bluetooth reaches BLE ones, and a browser may have either,
   * both, or neither. Saying which is missing beats a chooser that opens empty.
   */
  function announce() {
    var hasSerial = "serial" in navigator;
    var hasBluetooth = "bluetooth" in navigator;

    if (!window.isSecureContext) {
      setStatus(strings.insecure);
      return;
    }
    if (!hasSerial && !hasBluetooth) {
      setStatus(strings.noTransport);
      return;
    }
    if (transportChoice) {
      Array.prototype.forEach.call(transportChoice.options, function (option) {
        var available = option.value === "bluetooth" ? hasBluetooth : hasSerial;
        option.disabled = !available;
        if (!available && transportChoice.value === option.value) {
          transportChoice.value = option.value === "bluetooth" ? "serial" : "bluetooth";
        }
      });
    }
    if (!hasSerial) {
      setStatus(strings.bluetoothOnly);
    } else if (!hasBluetooth) {
      setStatus(strings.serialOnly);
    } else {
      setStatus(strings.ready);
    }
  }

  announce();
})();
