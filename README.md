# Nex

[![Tests](https://github.com/xWray-Lp/Nex2/actions/workflows/tests.yml/badge.svg)](https://github.com/xWray-Lp/Nex2/actions/workflows/tests.yml)

> C kadar güçlü, Python kadar basit bir dil.

Nex, Python'a benzer girintili bir sözdizimi ile statik tip çıkarımını
birleştirmeyi, `@own` / `@raw` / `@stack` ile de C/Rust tarzı bellek
kontrolüne kaçış kapısı bırakmayı hedefleyen, sıfırdan Python ile
geliştirilen bir programlama dili.

Bu proje aktif geliştirme aşamasında, "proof of concept" niteliğinde.

## Hızlı örnek

```nex
fn factorial(n: int) -> int:
    if n == 0:
        return 1
    else:
        return n * factorial(n - 1)

fn main():
    print("10! = " + str(factorial(10)))

    let total = 0
    for i in 0..10:
        total = total + i
    print("Sum 0..9 = " + str(total))

main()
```

Daha fazla örnek için `examples/` klasörüne bakabilirsin:

- `hello.nex`, `fizzbuzz.nex` — temel dil akışı
- `stack.nex` — `@stack` sabit diziler
- `data_pipeline.nex` — `@own` + `to_own`/`to_gc` + borçlarla veri-işleme
- `tree_own.nex` — sınıflar, özyineleme, snapshot sahipliği (borç okuma/yazma)
- `buffer_econ.nex` — `@stack` döngüsel tampon + `@raw` imleçler (heap'siz ekonomi)

## Şu an gerçekten çalışan özellikler

Bu liste kod tabanı taranarak doğrulanmıştır (AST'de tanımlı olmak,
interpreter'da çalışıyor anlamına gelmez — aşağıdakiler gerçekten
`interpreter.py` içinde uygulanmış olanlardır):

- Değişkenler, fonksiyonlar, sınıflar (`class`), lambda
- `if`/`while`/`for`, `break`/`continue`
- `match` ifadeleri (pattern matching)
- Pipe operatörü (`|>`)
- Null coalescing (`??`)
- f-string interpolasyonu
- `try`/`catch`
- Liste, dict, tuple, set literal'leri, `range` (`0..10`)
- REPL (`main.py` üzerinden, `:check` komutuyla sadece tip kontrolü de yapılabilir)
- `@stack` sabit boyutlu diziler: `@stack let buf: [int; 4] = [0, 0, 0, 0]` —
  derleme zamanında boyut ve sabit indeks denetimi, çalışma zamanında değişken
  indeks denetimi; kaçış (GC değişkene, fonksiyona, `return`'e) heap liste
  kopyası üretir. `@` öneki opsiyoneldir (`stack let ...` de çalışır).
  Ayrıntı: [`NEX_MEMORY_MODEL.md`](./NEX_MEMORY_MODEL.md) Bölüm 6, madde 0
- `@own` sahipliği: `@own let p = to_own(x)` (GC'den derin kopya), `to_gc(p)`
  (GC'ye taşıma; sonrasında `p` geçersiz), `free(p)`; `&x` / `&mut x` borrow'ları
  blok sonunda otomatik bırakılır, çakışmalar runtime'da yakalanır. Borrow
  kullanıcı fonksiyonlarına geçemez; saf builtin'lerde (`len`, `keys`,
  `values`) `len(&x)` serbesttir.
  Ayrıntı: [`NEX_MEMORY_MODEL.md`](./NEX_MEMORY_MODEL.md) Bölüm 2/4/5

## Henüz gerçek davranışı olmayan (sözdizimi var, semantik yok)

- **`@raw` kısıtlaması** — uygulanmıştır: `@raw` değişkene GC/own kaynaklı
  değer (liste/dict/set/tuple/sınıf/string) atanması derleme hatasıdır;
  ilkel tipler ve `@stack` dizileri serbesttir. Runtime'da `@raw` değişken
  v1'de düz değer tutar (gerçek ham adres/aliasing VM dönemi işi).
- **`spawn` / `await`** — şu an ifadeyi olduğu gibi senkron değerlendiriyor,
  gerçek bir concurrency/scheduler yok.

## Mimari

```
Kaynak kod → Lexer → Parser → AST → Type Checker → Interpreter (tree-walking)
```

Bytecode VM'e geçiş bir sonraki büyük kilometre taşı olarak planlanıyor
(tree-walking interpreter, prototipleme için yeterli ama performans hedefi
için VM gerekiyor).

## Proje notları

- [`NEX_DEVNOT.md`](./NEX_DEVNOT.md) — vizyon ve genel roadmap
- [`NEX_MEMORY_MODEL.md`](./NEX_MEMORY_MODEL.md) — bellek modeli tasarımı,
  gerekçeler ve uygulama sırası
