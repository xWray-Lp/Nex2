# Nex Bellek Modeli — Tasarım ve Yol Haritası

> Bu doküman, "C kadar güçlü, Python kadar basit" hedefinin bellek yönetimi
> tarafını nasıl karşılayacağımızı, hangi kararların neden alındığını ve
> hangi adımların hangi sırayla uygulanacağını açıklar. Amaç: bir sonraki
> oturumda (biz ya da başka bir model) "neden böyle yaptık" sorusunun
> cevabını tekrar sıfırdan icat etmek zorunda kalmamak.

## 0. Mevcut durum (kod taraması ile doğrulandı)

> **Tarihsel not (güncellendi):** Aşağıdaki tarama anlık görüntüsü,
> `@stack`/`@own`/borrow uygulanmadan önceki durumu yansıtır; artık
> geçerliliğini yitirmiştir. **Güncel gerçeklik:** `@stack` ve `@own`
> (`to_own`/`to_gc`/`free`), `&x`/`&mut x` borrow'ları (v1) ve `@raw`
> statik kısıtı (§6 madde 5) uygulanmıştır.

- `ast_nodes.py` → `VarDecl.mem_mode: Optional[str]` alanı zaten var:
  `"own" | "raw" | "stack" | None`.
- `parser.py` (satır ~396-435) → `@own`, `@raw`, `@stack` annotation'larını
  gerçekten parse ediyor, AST'ye yazıyor.
- `type_checker.py` → `mem_mode` alanını **hiç okumuyor**. `alloc()` /
  `free()` builtin fonksiyonları var (satır ~109-110) ama bunlarla
  ownership/borrow arasında bir bağ yok.
- `interpreter.py` → `mem_mode` alanını **hiç kullanmıyor**. Her değişken
  aynı şekilde (Python'un kendi GC'sine bırakılarak) çalışıyor.

**Sonuç:** 4 mod sözdizimi seviyesinde tanımlı ama hiçbirinin gerçek bir
davranışı yok. Annotation yazmak şu an kozmetik — hiçbir şeyi değiştirmiyor.
Bu, kod tabanının doğrulanmış gerçek durumu; varsayım değil.

## 1. Neden 4 mod (ve neden bu isimler)

| Mod | Ne zaman kullanılır | Neden var |
|---|---|---|
| **GC** (varsayılan, annotation yok) | Genel amaçlı, "Python gibi" kod | Dilin varsayılan deneyimi kolay olmalı — programcı hiçbir şey yazmadan güvenli çalışan kod yazabilmeli |
| **`@own`** | Kaynak sahipliğinin net olması gereken yerler (dosya handle, bağlantı, büyük veri yapıları) | Rust'ın kanıtladığı model: deterministik temizlik (scope bitince free), GC duraklamasız |
| **`@raw`** | Performans-kritik, düşük seviye kod (buffer, sistem çağrıları) | "C kadar güçlü" hedefi için kaçış kapısı — programcı bilerek tüm sorumluluğu alır |
| **`@stack`** | Sabit boyutlu, kısa ömürlü veri (küçük dizi/buffer) | Heap'e hiç gitmeden en hızlı seçenek; GC ve ownership'ten tamamen bağımsız, en basit mod |

`@stack` diğer üçünden farklı bir kategoridedir: heap'e dokunmadığı için
GC/own/raw etkileşim sorunlarının **hiçbiri onu ilgilendirmez** — scope
bitince otomatik temizlenir, borrow/ownership kuralı gerekmez. Bu yüzden
aşağıdaki etkileşim tablosunda `@stack` satırı yok; o zaten "sorunsuz" mod.

## 2. Mod geçişleri (GC ⇄ own)

```
GC nesnesini doğrudan @own ile sahiplenmek  → COMPILE ERROR
GC nesnesini to_own(x) ile sahiplenmek      → DEEP COPY, yeni bağımsız @own nesne
@own nesneyi to_gc(x) ile GC'ye vermek       → MOVE, x own'da artık geçersiz, GC kaydına eklenir
```

- `free(x)` → sahipliği tüketir; `x` ölür (use-after-free). `x` hâlâ borrow
  altındayken `free`/taşıma yasaktır.
- `@own` değişkene `to_own(...)` sonucu yeniden atanırsa sahiplik yaşam
  döngüsü sıfırlanır (önceki moved/free durumu temizlenir).

**Neden deep copy (own'a girişte) ama move (own'dan çıkışta)?**
GC'nin elinde tuttuğu bir referans, başka yerlerden de erişiliyor olabilir
(GC = paylaşılan sahiplik felsefesi). Onu "taşımak" (move) GC tarafındaki
diğer referansları sessizce geçersiz kılar — bu, GC'nin paylaşılan-sahiplik
sözleşmesini bozar. Deep copy bu belirsizliği ortadan kaldırır: pahalıdır
ama güvenlidir. own→GC yönünde ise own zaten tekil sahipliğe sahip
olduğundan (kimse başka referans tutamaz), move güvenlidir ve gereksiz
kopyayı önler.

## 3. `@raw` kısıtlaması

```
@raw pointer, GC/own nesnesini gösteremez  → COMPILE ERROR
@raw pointer, başka @raw/@stack belleği gösterir → izin verilir, dangling riski programcıya ait
```

**Neden bu kısıtlama var?** GC nesneleri taşınabilir/toplanabilir; `@raw`
bir ham adrestir ve GC bunun farkında değildir. GC bir nesneyi taşıdığı
anda `@raw` pointer geçersiz bir adresi göstermeye devam eder (use-after-
free/dangling). Bunu derleme zamanında engellemek ucuzdur (tip kontrolü),
runtime'da GC'yi "pointer-farkında" yapmak ise GC tasarımını çok
karmaşıklaştırır. Bu yüzden en ucuz ve en güvenli çözüm: `@raw`'ı
kaynağında kısıtlamak.

> **v1 uygulama notu:** Kısıt statiktir — `@raw` bildirim ve atama
> noktalarında kaynak tipi denetlenir (ilkel + `@stack` dizisi serbest;
> `str` dahil tüm GC nesneleri red — string de heap nesnesidir). Runtime
> v1'de `@raw` değişken düz değer tutar (stack kaynağı heap kopyasına
> çevrilir); gerçek ham adres/aliasing semantiği VM dönemi işidir.

## 4. Borrow kuralları (`@own` için)

```
&x     → salt okunur ödünç, N tane aynı anda serbest
&mut x → yazılabilir ödünç, aynı anda sadece 1 tane, başka hiçbir borrow yasak
```

Bu, Rust'ın kanıtlanmış modeli — kendi kuralımızı icat etmek yerine
sektörde 10+ yıldır test edilmiş bir kuralı almak, hem daha az hata payı
bırakır hem de ileride dökümantasyon/örnek bulmayı kolaylaştırır (Rust
kaynakları büyük ölçüde uygulanabilir).

- **v1 (uygulandı):** Borrow'lar blok-yereldir; `Environment.close()` ile scope
  sonunda otomatik bırakılır (release-before-kill).
- **v1 sınırı:** Borrow referansı fonksiyon sınırını geçemez; VM sonrası gerçek
  kısmi geçiş (parametre olarak borrow) ertelenmiştir.
- **v1 istisnası (allowlist):** Saf/yapısal builtin'ler (`len`, `keys`,
  `values`) referansı yalnız çağrı süresince okur, saklamaz — bu yüzden
  `len(&x)`, `keys(&d)`, `values(&d)` serbesttir. Liste checker'da
  `PURE_BORROW_BUILTINS`, runtime'da aynı adlı sabit ile tutulur; iki taraf
  birlikte güncellenmelidir. Kullanıcı fonksiyonları ve diğer builtin'ler
  (`str(&x)` gibi) hâlâ sınır kuralına tabidir.

## 5. Uygulama katmanı: neden runtime + statik ikisi birden

Tam bir compile-time borrow checker (Rust tarzı lifetime analizi +
control-flow graph) **solo bir projede aylarca sürecek bağımsız bir
mühendislik fazıdır.** Ama borrow ihlallerinin büyük kısmı aslında basit:
aynı fonksiyon/blok içinde art arda gelen ihlaller. Bu yüzden iki katman:

### Katman 1 — Basit statik kontrol (type_checker.py içinde, kısa vadede)
- Kapsam: **tek blok/scope içi** borrow ihlalleri.
- `type_checker.py`'daki mevcut `Scope` sınıfı zaten AST'yi blok blok
  geziyor (`visit_Block`, satır 274) — buraya her `@own` değişken için
  "şu an aktif borrow'lar" listesi eklemek mevcut mimariye doğal
  şekilde oturur. Yeni bir alt sistem kurmuyoruz, var olanı genişletiyoruz.
- Yakalayamadığı şey: fonksiyonlar arası geçen borrow'lar, döngü/koşul
  dallarına yayılan borrow'lar, closure'lar.
- Tahmini iş büyüklüğü: küçük–orta (mevcut `Scope` sınıfına yeni bir alan
  ve birkaç kontrol fonksiyonu).

### Katman 2 — Runtime borrow flag (interpreter.py içinde, güvenlik ağı)
- Her `@own` değer, çalışma zamanında bir durum taşır:
  `Free | Borrowed(n) | MutBorrowed`.
- Borrow alma/bırakma anında bu flag kontrol edilir; ihlalde runtime
  hata (panic) fırlatılır.
- Katman 1'in kaçırdığı her şeyi (çapraz fonksiyon, döngü, closure) yakalar.
- **Uygulama durumu adları:** `alive=False` → `Free`, `shared=n` →
  `Borrowed(n)`, `mut_borrowed=1` → `MutBorrowed` (`OwnValue` alanları).
- Rust'ta bile tam karşılığı yok gibi görünse de aslında `RefCell` /
  `Rc<RefCell<T>>` runtime borrow-checking olarak tam olarak bunu yapar —
  yani "runtime'da borrow kontrolü" sektörde de kabul görmüş bir desen.

**Neden ikisi birden ve neden bu sıra:**
Katman 1 tek başına yetersiz (çoğu gerçek ihlal fonksiyonlar arası olur).
Katman 2 tek başına yetersiz (hataları derleme yerine çalışma zamanında,
geç yakalar — "C kadar güçlü" hedefiyle çelişir, güvenlik geç gelir).
İkisi birlikte: basit hatalar erken (derleme benzeri aşamada), karmaşık
hatalar geç ama yine de güvenli şekilde yakalanır. Katman 1, ileride
bytecode VM'e geçilince gerçek bir CFG/lifetime analizine genişletilebilir
— sıfırdan başlamak yerine üzerine inşa edilir.

## 6. Yol haritası (uygulama sırası)

0. **`@stack`** — GC/ownership/raw ile hiçbir etkileşimi olmadığı için en
   düşük riskli, en hızlı kazanılan adım (dış görüş / Qwen incelemesiyle
   de doğrulandı). Python tarafında sabit boyutlu bir liste/array olarak
   simüle edilebilir; scope bitince otomatik serbest bırakılır. Diğer
   maddelerden bağımsız olarak, istenildiğinde en önce yapılabilir.
   **v1 uygulandı:** `[T; N]` tip sözdizimi + `StackArrayType` (tip
   sistemi) + `StackArray` (interpreter); boyut ve sabit indeks
   denetimleri derleme zamanında, değişken indeks denetimi çalışma
   zamanında. Kaçış kuralı: stack dizi değer olarak kapsamdan çıkarsa
   heap liste kopyasına dönüşür. Testler `test_stack.py` içinde,
   örnek `examples/stack.nex` içinde. Boyut üst sınırı v1'de yok
   (yalnızca `N >= 1`); üst sınır ertelenmiş karar olarak Bölüm 7'de
   duruyor.

   **v2 düzeltmeleri (bağımsız denetim turu sonrası):** v1'in kaçış
   kuralı tüm kaçış noktalarında tutarlı değildi. Yapılan düzeltmeler:
   (1) `to_list()` yüzeysel kopyadan derin kopyaya çevrildi — iç içe
   stack/list değerleri artık bağımsız; (2) kaçış noktaları tam
   yönetildi: liste/dict/set/tuple literal'leri, sınıf alanı ve dict
   atamaları, `|>` operatörü, fonksiyon default parametreleri, match
   konusu; (3) `StackPanic` fırlatılmayan dead-scope hataları artık
   `NexRuntimeError` alt sınıfı — `catch` yakalar, "Internal Error"
   yerine konumlu hata basılır; (4) checker/runtime metot drift'i
   giderildi (`isEmpty`/`indexOf` implement edildi, `contains`/`join`
   closure'ları dead-scope denetimli); (5) checker'ın eleman tipi ve
   iç boyut denetimleri eklendi (`_check_stack_literal`, element
   assignment tip denetimi); (6) kaçış bahsinde statik tip `ListType`
   çıkarımı yapılır (`let xs = buf; xs.push(3)` artık meşru).
1. **`nex_types.py`**: `@own` değerler için bir `OwnState` (borrow sayacı +
   mod: none/shared/mut) kavramı tanımla. **v1 uygulandı:**
   `nex_types.OwnType` + `interpreter.OwnValue`
   (`alive`/`shared`/`mut_borrowed`).
2. **`interpreter.py`**: Katman 2 (runtime borrow flag) — en önce bu,
   çünkü hemen test edilebilir sonuç verir, ileri geri bildirim döngüsü
   kısa.
3. **`type_checker.py`**: Katman 1 (blok-içi statik kontrol) — mevcut
   `Scope`'a borrow takibi ekle. **Kısmen:** yalnız operand/mode/sınır
   denetimi statik; çakışma denetimi runtime'da (Katman 2),
   CFG/Lifetime analizi VM sonrası.
4. **`to_own()` / `to_gc()`**: builtin olarak `alloc`/`free` yanına ekle,
   GC↔own geçiş kurallarını uygula.
5. **`@raw` kısıtlaması**: type_checker'da, bir `@raw` değişkene GC/own
   kaynaklı bir değer atanmaya çalışıldığında hata üret. **Uygulandı (v1):**
   `TypeChecker._check_raw_source` — bildirim ve atamada kaynak denetimi;
   ilkel + stack dizisi serbest, `str` dahil tüm GC nesneleri derleme
   hatası (runtime v1'de raw değişken hâlâ düz değer tutar; gerçek ham
   adres/aliasing VM dönemi işi).
6. **(Bytecode VM sonrası)** Katman 1'i gerçek CFG/lifetime analizine
   genişlet — fonksiyonlar arası, döngü/dal duyarlı borrow checker.
   **VM iskeleti:** barınma, GC yönü, bytecode şekli ve borrow/NLL
   yerleşimi `NEX_VM_DESIGN.md` içinde; bu madde orada compiler-side
   liveness olarak konumlanır (yeni VM opcode'u gerektirmez).

Sıralamanın mantığı: önce en hızlı geri bildirim veren (runtime), sonra
onu destekleyen statik katman, sonra geçiş fonksiyonları, en son (VM
aşamasında) en pahalı/karmaşık iş. `@stack` bu sıranın dışında çünkü
diğerlerine bağımlı değil — ayrı bir "hızlı kazanım" olarak istenilen
noktada araya sokulabilir.

## 7. Açık kalan sorular (bilerek ertelendi)

- `@stack` için boyut üst sınırı: v1'de yok (yalnızca `N >= 1` denetimi
  var). Bir üst sınır konulup konulmayacağı, konulursa değerinin ne
  olacağı VM aşamasında netleşecek; boyutun kendisinin derleme
  zamanında literal olması v1'den beri zorunlu.
- **CEVAPLANDI — `&mut` fonksiyon parametresi (v1):** Borrow referansı
  fonksiyon sınırını geçemez; VM sonrası gerçek kısmi geçiş (parametre
  olarak borrow) ertelenmiştir. Blok-yerel borrow, scope sonunda
  `Environment.close()` ile otomatik bırakılır. `&mut x` parametresi
  derleme hatasıdır; paylaşımlı `&x` parametresi derlenir (değer güncel
  parametre olarak geçer). Not: `&` sonrası `mut` anahtar kelimesidir —
  adı tam `mut` olan parametreye `&` üzerinden referans verilemez.
- **CEVAPLANDI — closure capture:** Yakalanan `@own` değer scope kapanışında
  block-local kill ile ölür; closure içinden sonraki erişim panic üretir
  (use-after-free).
- **Python'un kendi GC'si ile çakışma riski** (dış görüş / Qwen
  incelemesiyle tespit edildi): Nex şu an Python üzerinde yorumlanıyor.
  `@own`/`@raw` nesneleri de sonuçta birer Python objesi olacak — yani
  Python'un kendi referans sayımı + döngüsel GC'si arka planda zaten
  çalışıyor olacak. Nex'in kendi ownership/borrow mantığı bunun **üzerine**
  bindirilecek, yerine geçmeyecek. Bu iki katmanın çakışabileceği somut
  senaryo: Nex tarafında "bu nesne artık kimsenin değil" dediğimiz an ile
  Python'un o nesneyi gerçekten belleğe geri vermesi (refcount 0 olması)
  aynı anda gerçekleşmeyebilir — yani Nex'in "deterministik temizlik"
  vaadi, altında Python GC'si çalıştığı sürece **tam deterministik
  olmayabilir**. Bytecode VM'e geçilene kadar (kendi bellek yönetimimizi
  yazana kadar) bu, çözülemeyecek yapısal bir sınırlama — şimdilik
  bilinçli olarak kabul ediliyor, "çözüldü" değil.

Bunlar Katman 1/2 uygulanırken netleşecek; şimdiden cevap uydurmuyoruz.
