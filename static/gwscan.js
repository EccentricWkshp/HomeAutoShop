/*
 * Reading a GEARWRENCH GWSCAN (XTOOL AD20) over Bluetooth LE.
 *
 * The adapter advertises Nordic UART, which the code reader already connects
 * to, and then answers no ELM327 command at all: that profile is a pipe, and
 * what goes through it is the vendor's own protocol. This is that protocol.
 *
 *     marker  SEQ  LEN  KIND  PAYLOAD[LEN]  XOR(SEQ..PAYLOAD)
 *
 * `marker` is AA outbound and 55 inbound, and it never occurs inside a frame:
 * the stream is byte-stuffed, with the escape being the marker plus one. LEN
 * and the checksum are both counted on the *unstuffed* bytes — the detail that
 * makes a first implementation look like a flaky link rather than a wrong
 * reader.
 *
 * **The setup frames are not invented here.** They come from the server, out
 * of `homeautoshop/diagnostics/gwscan.py`, which is where they are tested and
 * where the note about which of them are understood lives. This file is the
 * codec and the conversation; the vocabulary is data.
 *
 * What comes back is ordinary OBD-II inside a thin wrapper, so this hands
 * `elm327.js` the same hex text an ELM327 would have printed and every decoder
 * downstream is the one that already exists.
 */
(function () {
  "use strict";

  var HOST = 0xAA;
  var ADAPTER = 0x55;
  var CLASS = 0x60;
  var REQUEST_ID = 0x7DF;

  var SEND_CAN = 0x09;
  var GOT_CAN = 0x0A;

  var FIRMWARE = [0x02, 0x01, 0x81];

  function hexToBytes(hex) {
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i++) {
      out[i] = parseInt(hex.substr(i * 2, 2), 16);
    }
    return out;
  }

  function concat(a, b) {
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function checksum(bytes) {
    var value = 0;
    for (var i = 0; i < bytes.length; i++) { value ^= bytes[i]; }
    return value;
  }

  /* The marker cannot appear inside a frame, so it is escaped on the way out
   * and put back on the way in. Both halves live here, together, because a
   * stuffing rule and an unstuffing rule that disagree fail intermittently and
   * only on the payloads that happen to contain the marker. */
  function stuff(bytes, marker) {
    var escape = (marker + 1) & 0xFF;
    var out = [];
    for (var i = 0; i < bytes.length; i++) {
      if (bytes[i] === marker) { out.push(escape, 0x02); }
      else if (bytes[i] === escape) { out.push(escape, 0x01); }
      else { out.push(bytes[i]); }
    }
    return new Uint8Array(out);
  }

  /*
   * Unstuff at most `count` bytes, reporting how much input that took.
   *
   * `count` is what a live reader needs and a capture does not: a frame states
   * its own length, so exactly that much is decoded and the next frame starts
   * where this one stopped. Fewer bytes than asked for means the rest has not
   * arrived.
   *
   * A lone escape at the end is not consumed — on a live link it is half of a
   * pair whose other half is still in flight.
   */
  function unstuffUpto(bytes, marker, count) {
    var escape = (marker + 1) & 0xFF;
    var out = [];
    var i = 0;
    while (i < bytes.length && (count === undefined || out.length < count)) {
      if (bytes[i] === escape) {
        if (i + 1 >= bytes.length) { break; }
        if (bytes[i + 1] === 0x02) { out.push(marker); i += 2; continue; }
        if (bytes[i + 1] === 0x01) { out.push(escape); i += 2; continue; }
      }
      out.push(bytes[i]);
      i += 1;
    }
    return { bytes: new Uint8Array(out), consumed: i };
  }

  function build(seq, payload, marker) {
    marker = marker === undefined ? HOST : marker;
    var body = new Uint8Array(3 + payload.length);
    body[0] = seq & 0xFF;
    body[1] = payload.length;
    body[2] = CLASS;
    body.set(payload, 3);
    var whole = concat(body, new Uint8Array([checksum(body)]));
    return concat(new Uint8Array([marker]), stuff(whole, marker));
  }

  /*
   * Every whole frame in a stream, and how much of it was used.
   *
   * A frame ends where its length says, not where the next marker starts.
   * Waiting for a marker would mean never seeing an answer until the following
   * one arrives, which on a device that answers one question at a time is a
   * reader that never sees anything.
   */
  function parse(stream, marker) {
    marker = marker === undefined ? ADAPTER : marker;
    var frames = [];
    var consumed = 0;
    var i = 0;
    while (i < stream.length) {
      if (stream[i] !== marker) { i += 1; consumed = i; continue; }
      var rest = stream.subarray(i + 1);
      var head = unstuffUpto(rest, marker, 3);
      if (head.bytes.length < 3) { break; }
      var want = 3 + head.bytes[1] + 1;
      var body = unstuffUpto(rest, marker, want);
      if (body.bytes.length < want) { break; }
      var b = body.bytes;
      frames.push({
        seq: b[0],
        kind: b[2],
        payload: b.subarray(3, b.length - 1),
        checksumOk: checksum(b.subarray(0, b.length - 1)) === b[b.length - 1]
      });
      i = i + 1 + body.consumed;
      consumed = i;
    }
    return { frames: frames, consumed: consumed };
  }

  /* -- what goes inside a frame ------------------------------------------ */

  function service(mode, pid) {
    var body = pid === undefined ? [mode] : [mode, pid];
    return new Uint8Array([body.length].concat(body));
  }

  function canRequest(data, canId) {
    canId = canId === undefined ? REQUEST_ID : canId;
    var out = new Uint8Array(13);
    out[0] = SEND_CAN;
    out[1] = 0x0B;
    out[2] = 0x08;
    out[3] = (canId >> 8) & 0xFF;
    out[4] = canId & 0xFF;
    out.set(data.subarray(0, 8), 5);
    return out;
  }

  function canReply(payload) {
    if (payload.length < 13 || payload[0] !== GOT_CAN) { return null; }
    return {
      id: (payload[3] << 8) | payload[4],
      data: payload.subarray(5, 13)
    };
  }

  /*
   * The OBD-II response inside a single-frame CAN reply.
   *
   * The first byte is the ISO-TP length and the rest is padding. A first or
   * consecutive frame — a multi-frame reply — returns nothing rather than its
   * first eight bytes: no capture has a car with enough stored codes to send
   * one, and a list of codes that is quietly short is worse than none.
   */
  function obdBytes(data) {
    if (!data.length) { return new Uint8Array(0); }
    if ((data[0] >> 4) !== 0) { return new Uint8Array(0); }
    var length = data[0] & 0x0F;
    return data.subarray(1, 1 + length);
  }

  function asElmText(data) {
    var body = obdBytes(data);
    var out = [];
    for (var i = 0; i < body.length; i++) {
      out.push(("0" + body[i].toString(16).toUpperCase()).slice(-2));
    }
    return out.join(" ");
  }

  /* `7F 0A 11` is *service not supported*, which is not an empty list of
   * codes. Showing the two the same way tells somebody their car is clean when
   * nobody asked it anything. */
  function negative(data) {
    var body = obdBytes(data);
    if (body.length >= 3 && body[0] === 0x7F) {
      return { mode: body[1], reason: body[2] };
    }
    return null;
  }

  /* -- the conversation -------------------------------------------------- */

  /*
   * `io` is the transport: `write(bytes)` and `read()`, where read returns
   * whatever has arrived since it was last called. Keeping the buffer here
   * rather than in the transport means the framing and the reassembly are one
   * piece of code with one set of tests.
   */
  function Session(io) {
    this.io = io;
    this.buffer = new Uint8Array(0);
    this.seq = 1;
  }

  Session.prototype.next = function () {
    this.seq = (this.seq + 1) & 0xFF;
    // The marker's own value as a sequence number is legal — it is escaped
    // like any other byte — but it makes a log much harder to read.
    if (this.seq === HOST || this.seq === ADAPTER) { this.seq += 1; }
    return this.seq;
  };

  Session.prototype.ask = async function (payload, timeoutMs) {
    var seq = this.next();
    this.buffer = new Uint8Array(0);
    await this.io.write(build(seq, payload));
    var deadline = Date.now() + (timeoutMs || 3000);
    while (Date.now() < deadline) {
      this.buffer = concat(this.buffer, this.io.read());
      var got = parse(this.buffer);
      this.buffer = this.buffer.subarray(got.consumed);
      for (var i = 0; i < got.frames.length; i++) {
        if (got.frames[i].seq === seq && got.frames[i].checksumOk) {
          return got.frames[i];
        }
      }
      await new Promise(function (done) { window.setTimeout(done, 30); });
    }
    return null;
  };

  /*
   * Say hello, set the bus up, and ask the three code services.
   *
   * The hello is not ceremony: reading the firmware version is the first
   * evidence that the protocol is right at all, and the alternative symptom —
   * for an adapter that answers nothing to anything — is indistinguishable
   * from a flat battery.
   */
  async function readCodes(io, options) {
    var session = new Session(io);
    var log = (options && options.log) || function () { };
    var setup = (options && options.setup) || [];
    var modes = (options && options.modes) || [0x03, 0x07, 0x0A];

    var hello = await session.ask(new Uint8Array(FIRMWARE), 4000);
    if (!hello) { return null; }
    log("hello", hello.payload);

    for (var i = 0; i < setup.length; i++) {
      var step = setup[i];
      var acked = await session.ask(hexToBytes(step.payload), 4000);
      log("setup", step.what, !!acked);
    }

    var results = [];
    for (var m = 0; m < modes.length; m++) {
      var reply = await session.ask(canRequest(service(modes[m])), 8000);
      if (!reply) {
        results.push({ mode: modes[m], silent: true, text: "" });
        continue;
      }
      var received = canReply(reply.payload);
      if (!received) {
        results.push({ mode: modes[m], silent: true, text: "" });
        continue;
      }
      results.push({
        mode: modes[m],
        silent: false,
        refused: negative(received.data),
        text: asElmText(received.data)
      });
    }
    return { firmware: hello.payload, results: results };
  }

  window.homeautoshop = window.homeautoshop || {};
  window.homeautoshop.gwscan = {
    HOST: HOST,
    ADAPTER: ADAPTER,
    hexToBytes: hexToBytes,
    stuff: stuff,
    unstuffUpto: unstuffUpto,
    build: build,
    parse: parse,
    service: service,
    canRequest: canRequest,
    canReply: canReply,
    obdBytes: obdBytes,
    asElmText: asElmText,
    negative: negative,
    Session: Session,
    readCodes: readCodes
  };
})();
