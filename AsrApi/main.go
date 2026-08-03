package main

import (
	"embed"
	"log"

	"github.com/aiursoftweb/asr-api/internal/server"
)

//go:embed web/dist
var distFS embed.FS

func main() {
	if err := server.Run(distFS); err != nil {
		log.Fatal(err)
	}
}
