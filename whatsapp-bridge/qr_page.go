package main

import (
	"encoding/base64"
	"flag"
	"fmt"
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"

	"rsc.io/qr"
)

// CLI flag: show the pairing QR code in the default web browser.
//
// The terminal QR is drawn with Unicode half-block characters. Some terminals
// (Windows consoles with fonts that leave gaps between lines, terminals that
// are not a TTY such as Git Bash/mintty, or a window too short for the code)
// render it unscannable, and the DEBUG log output printed during pairing can
// interleave with it or scroll it away. The browser page shows a real image
// instead and reloads itself, so it always shows the current code.
var qrBrowserFlag = flag.Bool("qr-browser", false,
	"Also show the pairing QR code in your web browser (a self-refreshing page written to store/pairing-qr.html)")

const pairingQRPagePath = "store/pairing-qr.html"

// pairingQRPage keeps store/pairing-qr.html in sync with the pairing QR code.
// The page holds a short-lived pairing secret, so it is written owner-only
// inside store/ and replaced with a secret-free page as soon as pairing ends.
type pairingQRPage struct {
	path   string
	open   func(path string) error
	mu     sync.Mutex
	opened bool
}

// show writes the page for code and opens it in the browser the first time
// in each pairing attempt. Later codes only rewrite the file; the page's
// meta refresh picks them up.
func (p *pairingQRPage) show(code string) error {
	qrCode, err := qr.Encode(code, qr.L)
	if err != nil {
		return fmt.Errorf("encode QR code: %w", err)
	}
	qrCode.Scale = 8
	img := base64.StdEncoding.EncodeToString(qrCode.PNG())
	body := `<h1>Link WhatsApp</h1>
<p>On your phone: WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device, then scan this code.</p>
<img alt="WhatsApp pairing QR code" src="data:image/png;base64,` + img + `">
<p class="note">The code refreshes automatically. This page updates every 2 seconds.</p>`

	p.mu.Lock()
	defer p.mu.Unlock()
	if err := writePairingQRPage(p.path, body, true); err != nil {
		return err
	}
	if p.opened || p.open == nil {
		return nil
	}
	abs, err := filepath.Abs(p.path)
	if err != nil {
		return err
	}
	if err := p.open(abs); err != nil {
		return fmt.Errorf("open %s in browser: %w", abs, err)
	}
	p.opened = true
	return nil
}

// finish replaces the QR with a final, secret-free message. The page stops
// refreshing on a successful link; otherwise it keeps refreshing so a new
// pairing attempt (POST /api/connection/reconnect) shows up in the same tab.
func (p *pairingQRPage) finish(linked bool) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.opened {
		return nil
	}
	p.opened = false
	if linked {
		return writePairingQRPage(p.path, `<h1>Phone linked</h1>
<p>WhatsApp is linked. History sync continues in the bridge; you can close this tab.</p>`, false)
	}
	return writePairingQRPage(p.path, `<h1>QR code expired</h1>
<p>Pairing was not completed. Restart the bridge (or POST /api/connection/reconnect) to get a new code; this page will show it.</p>`, true)
}

func writePairingQRPage(path, body string, refresh bool) error {
	meta := ""
	if refresh {
		meta = `<meta http-equiv="refresh" content="2">`
	}
	// Name the bridge by its working directory (the parent of store/), so
	// several bridges pairing at once can be told apart.
	bridgeDir := mustAbs(filepath.Dir(filepath.Dir(mustAbs(path))))
	label := html.EscapeString(filepath.Base(bridgeDir))
	page := `<!doctype html>
<html><head><meta charset="utf-8">` + meta + `<title>WhatsApp pairing: ` + label + `</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f4f4;color:#111;text-align:center;margin:2em 1em}
img{background:#fff;padding:8px;image-rendering:pixelated;max-width:90vw;height:auto}
.note,.bridge{color:#555;font-size:.9em}
</style></head><body>
<p class="bridge">Bridge: <code>` + html.EscapeString(bridgeDir) + `</code></p>
` + body + `
</body></html>
`
	// Write then rename so a reload never sees a half-written file.
	tmp := path + ".tmp"
	if err := writeOwnerOnlyFile(tmp, []byte(page)); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// mustAbs returns the absolute form of path, or path itself if that fails.
func mustAbs(path string) string {
	if abs, err := filepath.Abs(path); err == nil {
		return abs
	}
	return path
}

// openInBrowser opens a local file with the system's default handler. The
// path is always the bridge's own page under store/, never caller input.
func openInBrowser(path string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", path)
	case "darwin":
		cmd = exec.Command("open", path)
	default:
		cmd = exec.Command("xdg-open", path)
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	go func() { _ = cmd.Wait() }()
	return nil
}
