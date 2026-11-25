package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
)

func TestPairingQRPageOpensOncePerAttemptAndDropsSecret(t *testing.T) {
	path := filepath.Join(t.TempDir(), "store", "pairing-qr.html")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	var opened []string
	page := &pairingQRPage{path: path, open: func(p string) error { opened = append(opened, p); return nil }}

	read := func() string {
		t.Helper()
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return string(data)
	}

	if err := page.show("first-code"); err != nil {
		t.Fatal(err)
	}
	first := read()
	if !strings.Contains(first, "data:image/png;base64,") || !strings.Contains(first, `http-equiv="refresh"`) {
		t.Fatalf("page lacks QR image or refresh: %s", first)
	}
	if err := page.show("second-code"); err != nil {
		t.Fatal(err)
	}
	if read() == first {
		t.Fatal("refreshed code did not rewrite the page")
	}
	if len(opened) != 1 || !filepath.IsAbs(opened[0]) {
		t.Fatalf("want one open with an absolute path, got %v", opened)
	}

	if err := page.finish(true); err != nil {
		t.Fatal(err)
	}
	done := read()
	if strings.Contains(done, "data:image") || strings.Contains(done, "refresh") || !strings.Contains(done, "Phone linked") {
		t.Fatalf("linked page still holds the QR or refreshes: %s", done)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("temporary file left behind")
	}

	// A new pairing attempt opens the browser again.
	if err := page.show("third-code"); err != nil {
		t.Fatal(err)
	}
	if len(opened) != 2 {
		t.Fatalf("new attempt did not reopen the page, opens: %d", len(opened))
	}
	if err := page.finish(false); err != nil {
		t.Fatal(err)
	}
	if expired := read(); strings.Contains(expired, "data:image") || !strings.Contains(expired, "expired") {
		t.Fatalf("expired page still holds the QR: %s", expired)
	}
}

func TestConnectionReportsPairingEnd(t *testing.T) {
	for _, test := range []struct {
		final  whatsmeow.QRChannelItem
		linked bool
	}{
		{whatsmeow.QRChannelSuccess, true},
		{whatsmeow.QRChannelTimeout, false},
	} {
		c, codes, _ := pairingFixture(t)
		c.authTimeout = 10 * time.Millisecond
		ended := make(chan bool, 2)
		c.pairingEnded = func(linked bool) { ended <- linked }
		c.reconnect()
		codes <- whatsmeow.QRChannelItem{Event: "code", Code: "secret", Timeout: time.Minute}
		waitConnection(t, c, "pairing")
		codes <- test.final
		select {
		case got := <-ended:
			if got != test.linked {
				t.Fatalf("%s: linked=%v, want %v", test.final.Event, got, test.linked)
			}
		case <-time.After(time.Second):
			t.Fatalf("%s: pairingEnded not called", test.final.Event)
		}
		c.close()
		if len(ended) != 0 {
			t.Fatalf("%s: pairingEnded called more than once", test.final.Event)
		}
	}
}
