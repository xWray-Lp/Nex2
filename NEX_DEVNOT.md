# Nex - Geliştirici Notları

## Vizyon
C gibi güçlü, Python gibi kolay bir dil.
- Güç: `@own`/`@raw` ile manuel bellek kontrolü, bytecode VM ile performans
- Kolaylık: Python benzeri sözdizimi, statik tip çıkarımı (elle tip yazmaya gerek yok)

## Roadmap

### Bytecode VM sonrası
- Kullanıcı dostu hata mesajları: konum + neden + öneri içeren, sadece satır numarası
  fırlatmak yerine (Rust tarzı "expected X, found Y, did you mean...") diagnostics sistemi.
