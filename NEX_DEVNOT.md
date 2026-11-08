# Nex - Geliştirici Notları

## Vizyon
C gibi güçlü, Python gibi kolay bir dil.
- Güç: `@own`/`@raw` ile manuel bellek kontrolü, bytecode VM ile performans
- Kolaylık: Python benzeri sözdizimi, statik tip çıkarımı (elle tip yazmaya gerek yok)

## Roadmap

### Diagnostics (hata mesajları)
- **Artık var:** konum + sebep + basit öneri (checker `dosya:satır:kolon`,
  runtime `satır:kolon`).
- **Kalan (VM sonrası):** tüm builtin hatalarının konumlu olması + Rust tarzı
  "did you mean..." önerileri.
