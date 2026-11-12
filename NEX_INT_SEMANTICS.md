# Nex Tamsayı Semantiği — i64 Taşma (v0.1)

> Kısa karar notu. Amaç: `int` aritmetiğinin tree-walker ve VM'de konumlu ve
> tutarlı şekilde hata vermesi; backend kapsam farkları bu belgede açıkça
> belirtilir. Stil ve kapsam için bkz. `NEX_MEMORY_MODEL.md`.

## Temel karar

- **`int` = i64**: değer aralığı `[-2^63, 2^63-1]`.
- **Taşma TRAP eder**: aralık dışı sonuç, konumlu çalışma zamanı hatasına
  dönüşür: `Runtime Error at line L:C — integer overflow` (tree-walker) / VM
  tarafında eşdeğer konumlu `integer overflow` iletisi.
- **Sıfıra bölme** (`/`, `%` ve bileşik `/=`, `%=`) her iki backend'de konumlu
  `Division by zero` hatasıdır (i64 denetiminden bağımsız).

## Kapsanan işlemler

Aynı i64 denetimi bu işlemlere uygulanır (bool, bkz. aşağı):

- `+`, `-`, `*` — sonuç sonrası aralık denetimi.
- Tekli `-` — `-(-2^63)` taşar.
- `/` ve `%` — sıfıra bölme trap eder; ayrıca `INT_MIN / -1` ve
  `INT_MIN % -1` taşma trap'idir. `%` sonucu da aralık denetimine tabidir
  (örn. aralık dışı değer üreten durumlar trap eder).
- Bileşik atamalar: `+=`, `-=`, `*=`, `/=`, `%=` — aynı operatör mantığına
  yönlenir.

Notlar:
- `/` `float` üretir; bu nedenle normal bölme sonucu i64 aralık denetimine girmez
  (yalnız `INT_MIN / -1` özel durumu trap eder).
- `//` (tree-walker) `int(...)` sonucu üretir ve taşma denetimine tabidir;
  `INT_MIN // -1` ve aralık dışı sonuçlar trap eder.
- `%` tam sayı yolunda `_checked_int` ile aralık denetimi uygulanır.

## Backend kapsamı / bilinen prototip basitleştirmeleri

(a) **Bileşik atama taşma denetimleri** tree-walker'da uygulanır. VM
derleyicisinde bileşik atama (`+=` vb.) henüz desteklenmez (önceden var olan
sınırlama); bu yollar VM'de çalışmaz.
(b) **`//` (taban bölme) taşma işleme** tree-walker'a özgüdür; VM'de taban
bölme opcode'u yoktur.
(c) **`/` float üretir** (ve `//` tree-walker'da `int(...)` sonucu üzerinden
taşma denetimine tabidir); normal bölme sonucu i64 olarak overflow edemez.
(d) **Bool, i64 taşma muhasebesine girmez**: `INT_MAX + true` gibi bool
karışımları trap etmez ve i64 aralığı dışında kalabilen bir değer
üretebilir. Bu, i64-bool-dışlama kararına uygun, Python-prototipi kaynaklı bir
durumdur ve Faz 2'de kapatılacaktır (bool karışımının i64 semantiğine
katlanması/normalize edilmesi).
(e) **Rastgele hassasiyetli (arbitrary-precision) literaller** bilinçli bir
prototip basitleştirmesidir; Faz 2'de i64'a katlanır (literal/parse sınırı
ayrıca ele alınır). `%` sonucu artık aralık denetimli olduğundan, aralık dışı
literal kaynaklı örnekler (örn. `9223372036854775808 % 9223372036854775809`,
sonuç `9223372036854775808`, i64 dışı) beklenen şekilde trap eder.
(f) Tree-walker ve VM, destekledikleri ortak yollarda (temel aritmetik,
`INT_MIN/-1`, sınır değerler) aynı taşma/sıfıra-bölme davranışını hedefler;
ancak (a)/(b) ve prototip farkları nedeniyle tüm yüzeylerde birebir eşitlik
iddia edilmez.

## Kapsam dışı

- **`float`** etkilenmez.
- **String birleştirme** (`+`) etkilenmez.
- **Sarma (wrap) operatörleri ve genişlik dönüşümleri** v0.2 adaylarıdır.

## Gerekçe

Nex'in konumlu-hata disiplinine ve **Rust debug-modu** semantiğine uygundur
(C release modu değil). Gerekirse sarma davranışı, sessiz taşma yerine ileride
açık operatörlerle (`wrapping_add` benzeri) gelir.
