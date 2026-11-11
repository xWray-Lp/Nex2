# Nex Bytecode VM — Tasarım İskeleti (v0.1)

> Durum: **iskelet**. Bu belge VM döneminin tek tutarlı karar setini kurar;
> prototip değildir, alternatif karşılaştırması değildir. Buradaki kararlar
> kilitlenmeden prototip yazılmaz. Alternatifler (register machine, farklı GC
> aileleri, Rust port detayları) bu iskeletin *içinde* ayrı turlarda
> karşılaştırılır.
>
> İlgili: `NEX_MEMORY_MODEL.md` (§1 mod tablosu, §2 GC ⇄ own, §4 borrow,
> §6 yol haritası — madde 6 CFG/lifetime).

---

## 0. Kapsam ve kapsam dışı

**Kapsam:** bytecode formatı, yürütme modeli, frame/ownership yerleşimi,
borrow yaşam döngüsü, GC/allocator yönü, `@raw`'ın yeri, parity test stratejisi.

**Kapsam dışı (bilinçli):** JIT, inline cache, register allocation, eşzamanlılık,
modül sistemi, generiklerin VM temsili, optimizasyon geçişleri.

**Kural:** VM, dilin *mevcut v1 semantiğini* birebir korumakla yükümlüdür.
Dil semantiğini VM'e uydurmayız; VM'i semantiğe uydururuz. Her yeni VM
davranışı önce tree-walker'da referans semantik olarak doğrulanır.

---

## 1. Barınma: iki fazlı hedef (kilit karar)

| Faz | Barınma | Amaç | Ne değil |
|---|---|---|---|
| **1** | CPython-hosted VM | bytecode + ownership semantiğini doğrulamak, differential test | performans hedefi değil |
| **2** | Rust VM | gerçek allocator, gerçek adres, `@raw`'ın doğal evi | semantiği yeniden icat etmek değil |

**Gerekçe:** GC/allocator, ham pointer ve performans kararları ancak gerçek
bir host'ta anlamlıdır; ama bytecode ve ownership kuralları host'tan
bağımsızdır. Faz 1 bu bağımsız çekirdeği ucuza doğrular; Faz 2 aynı bytecode
sözleşmesini gerçek bellekle uygular.

**Sonuç (Faz 1 için bağlayıcı):**
- Bytecode içinde CPython'a özgü hiçbir şey yok: PyObject, id(), refcount yok.
- Host farkları tek bir **host arayüzü** arkasında toplanır:
  `alloc_gc`, `alloc_own`, `free_own`, `slot_read/write`, `call_native`.
- Faz 1'de "kendi allocator'ın" iddiası yoktur; GC tamamen host'a devredilir.
  Faz 1'in GC'si *semantik olarak* doğrudur, *performans olarak* değil.

---

## 2. Yürütme modeli: stack machine + açık frame (kilit karar)

**Karar:** stack tabanlı VM; operand stack + açık `Frame` yapısı. Register
machine değil.

**Gerekçe:**
- Tree-walker semantiğine en kısa eşleme; geçici değerler doğal olarak
  operand stack'te.
- Ownership/borrow metadata *register*'a değil *frame slot*'una aittir;
  stack machine bu ayrımı zorlamaz.
- Encoder/decoder ve verifier küçük kalır; hata ayıklama kolay.
- Register machine'e geçiş gerekirse aynı opcode sözleşmesi korunarak
  yapılabilir (operand stack, sanal register dosyasına eşlenir). Bu yüzden
  bu karar geri dönüşü olan bir karardır.

**Frame düzeni:**

```
Frame {
  fn:        FunctionRef,
  code:      &[u8],
  ip:        usize,
  locals:    [Slot],        // derleyicinin ayırdığı slot sayısı
  stack:     [Value],       // max_stack derleyici tarafından hesaplanır
  spill:     Vec<Value>,    // Faz 1: host tarafında, gerekirse
  ret:       Option<Value>,
  cleanup:   Vec<OwnSlot>,  // frame kapanışında ters sırada serbest bırakılır
  caller:    Option<FrameId>,
}
```

---

## 3. Değer ve mod temsili

Tek bir `Value` birleşimi; mod **ayrı tip evreni değil, slot etiketi**:

```
Value = Int64 | Float64 | Bool | Null
      | Str(Handle) | List(Handle) | Dict(Handle) | Tuple(Handle)
      | Instance(Handle) | Function(Handle)
      | StackRef(SlotId)      // @stack: frame içi gömülü tampon
      | RawWord(u64)          // @raw: Faz 1'de düz kelime, Faz 2'de adres

Slot { mode: GC | OWN | STACK | RAW | BORROW, value: Value, moved: bool }
```

Mod tablosu kaynak dille birebir:

| Kaynak | Slot mode | Sahiplik | Ömür |
|---|---|---|---|
| normal değer | `GC` | host GC | erişilebilirlik |
| `@own let` | `OWN` | frame slot'u | frame/blok kapanışı (deterministik) |
| `@stack let` | `STACK` | frame | frame |
| `@raw let` | `RAW` | yok | frame |
| `&x` / `&mut x` | `BORROW` | hedef slot | blok (v1) / son kullanım (NLL, §5) |

**Değişmezler (verifier + runtime):**
- `OWN` slota yalnız `own_alloc`/`own_move_in` yazar; GC değeri doğrudan yazılamaz.
- `OWN` değer `slot_read` ile okunamaz (v1 kuralı); yalnız borrow/transfer opcode'ları.
- `STACK` slot kopyalanamaz; kaçış noktasında `stack_materialize` (heap kopyası) gerekir.
- `moved` işaretli slottan okuma runtime hatası.

---

## 4. Sahiplik ve frame yaşam döngüsü

- `own` slotu tam olarak bir heap nesnesi sahiplenir. Frame kapanışında
  `cleanup` listesi **ters bildirim sırasıyla** boşaltılır (`own_free`).
- Blok çıkışı ayrı bir `cleanup` kapsamıdır: `block_enter`/`block_exit`
  opcode'ları o blokta açılan own/borrow kaynaklarını izler.
- `to_gc(x)` → `own_move` opcode'u:
  1. slotta aktif borrow var mı? (`shared_count`/`mut_flag`) varsa hata;
  2. sahipliği GC'ye devret (Faz 1: host handle'a bırak; Faz 2: nesneyi GC
     heap'ine taşı veya sahiplik bitini temizle);
  3. slotu `moved = true` yap.
- `to_own(x)` → `own_alloc` + derin kopya (Faz 1'de mevcut `_nex_deepcopy`
  semantiği referans alınır).
- `free(x)` → `own_free` + `moved = true`.

**Fonksiyon sınırı:** v1'de own/borrow sınırı geçemez. VM bunu *çağrı
opcode'unda* uygular: argüman slotlarında OWN/BORROW varsa `call` hatası.
Bu, checker'ın statik kuralının runtime güvenlik ağıdır. İleride gerçek
frame ömürleriyle bu kısıt gevşetilebilir; VM opcode seti değişmez, yalnız
verifier kuralı gevşer.

---

## 5. Borrow temsili ve NLL'nin yeri (kritik ayrım)

**v1 (Faz 1):** `BORROW` slotu ham pointer değil, **hedef slot kimliği +
nesil (generation)** tutar:

```
BorrowSlot { target_frame: FrameId, target_slot: SlotId, epoch: u32, mutable: bool, released: bool }
```

Erişimde `epoch` ve `released` denetlenir → use-after-free deterministik hata.
Faz 2'de aynı model adres + generation sayacına dönüşür; semantik aynı kalır.

**NLL'nin VM'e etkisi: SIFIR yeni opcode.**
Blok-kapsamlı borrow zaten `borrow_end` opcode'u ile temsil edilir. Gerçek
NLL (roadmap §6 madde 6) geldiğinde bu, *derleyici tarafında* bir CFG/liveness
geçişidir: `borrow_end`'i blok sonundan **son kullanıma** taşır. VM bu
instruction'ı aynen çalıştırır.

> Sonuç: Friction #1'in (blok sonuna kadar yaşayan borç → `to_gc` çakışması)
> VM'de kendiliğinden çözülmesi beklenir, çünkü CFG zaten yazılacak. Bu,
> v1'de `use buf { ... }` sugar'ının neden sadece geçici bir çözüm olduğunu
> da açıklar: asıl mekanizma compiler-side liveness'tır.

**Saf builtin istisnası:** `len`/`keys`/`values` gibi referansı saklamayan
native çağrılar, native fonksiyon tablosunda `retains_ref = false` bayrağıyla
işaretlenir. Checker'daki `PURE_BORROW_BUILTINS` allowlist'inin VM karşılığı
budur; verifier `retains_ref = true` olan native'e BORROW argümanını reddeder.

---

## 6. GC / allocator yönü

| Konu | Faz 1 (Python-hosted) | Faz 2 (Rust) |
|---|---|---|
| GC nesneleri | CPython refcount + cycle GC | **non-moving mark-sweep** (başlangıç) |
| Kökler | frame slotları, operand stack, globals, const pool | shadow stack / frame slotları |
| `@own` | VM sahipliği; host referansı serbest bırakılınca toplanır | size-class free list / sistem allocator |
| Taşıma (compaction) | yok | başlangıçta yok (ham adres kararlılığı için) |
| Finalizer | yok | yok |

**Neden non-moving:** `@raw` Faz 2'de gerçek adres taşıyacak; taşıyan GC
adres kararlılığını bozar. Compaction ileride yalnız `@raw`'ın yasaklandığı
bölgelerde veya handle-indirection ile düşünülebilir — bu iskeletin dışında.

**Neden mark-sweep:** döngüsel yapılar (ağaç/graph) v1'de GC modunda serbest;
refcount döngüyü sızdırır. Faz 2'de doğru başlangıç mark-sweep'tir.

**Risk sıralaması:** GC, beş başlığın en risklisi. Bu yüzden Faz 1'de GC
*hiç* yazılmaz; Faz 1'in işi bytecode/ownership'i doğrulamaktır. Faz 2'ye
geçiş kararı, parity testleri yeşile döndükten sonra verilir.

---

## 7. Bytecode formatı

**Modül:**

```
Header   { magic "NEXB", version u16, flags u16 }
ConstPool{ count, [Const] }           // int/float/str/...
FuncTable{ count, [FuncInfo] }
Code     { bytes }
Debug    { line_table per function, file names }

FuncInfo { name_idx, arity, n_locals, max_stack, code_off, code_len,
           line_table_off, flags }
```

**Encoding:** 1 bayt opcode + sabit genişlikte `u32` operand (v1'de basitlik;
ileride varint/compact encoding ayrı optimizasyon). Tüm operandlar little-endian.

> Ayrıntılı sözleşme (modül yerleşimi, sabit havuzu, tam opcode tablosu,
> verifier kuralları, line tablosu, örnek encoding ve disassembler biçimi):
> [`NEX_BYTECODE_SPEC.md`](./NEX_BYTECODE_SPEC.md).

**Opcode grupları (v1 çekirdeği):**

| Grup | Örnekler |
|---|---|
| sabit/slot | `const`, `load_local`, `store_local`, `load_global` |
| aritmetik/karşılaştırma | `add`, `sub`, `lt`, `eq`, `not` |
| kontrol | `jump`, `jump_if_false`, `loop_enter`, `loop_exit` |
| çağrı | `call`, `call_native`, `call_pure_native`, `ret` |
| nesne | `new_list`, `new_dict`, `get_index`, `set_index`, `get_member` |
| mod | `own_alloc`, `own_move`, `own_free`, `stack_alloc`, `stack_materialize`, `raw_make` |
| borrow | `borrow_shared`, `borrow_mut`, `borrow_check`, `borrow_end` |
| kapsam | `block_enter`, `block_exit` |

**Verifier (bytecode yüklenirken):** operand stack yüksekliği, jump hedefleri,
slot mod tutarlılığı, borrow kapsamı iç içeliği. Verifier hatası = program
çalışmaz (runtime'a düşmez).

**Disassembler:** `nexc --disasm` — differential test ve hata ayıklama için
zorunlu araç, ilk sprintte yazılır.

---

## 8. Parity / differential test stratejisi

Kabul kapısı: aynı `.nex` dosyası iki backend'de de aynı sonucu vermeli.

```
backend=treewalk  main.py program.nex  -> stdout, exit, diagnostics
backend=vm        main.py --vm program.nex -> stdout, exit, diagnostics
```

- Mevcut 3 süit (checker/own/stack) + `examples/` + `test_features.nex`
  ikinci backend ile de koşulur (CI matrisi).
- Bilinen farklar (ör. hata mesajı metni) beyaz listede tutulur; sessiz fark yok.
- Faz 1'in tamamlanma ölçütü: **tüm süitler VM backend'inde yeşil.**
- **Fuzz (v0.1 prototip):** 11k+ mutasyonlu decode/verify turunda beklenmeyen
  exception yok. Execute fuzz'ında iki sınır sınıfı doğrulandı: (a) bozulmuş
  `CALL` hedefi → çağrı derinliği sınırı (konumlu `VMError`, varsayılan 256);
  (b) bozulmuş `JUMP` döngüsü → sonlanma verifier'ın garanti edemeyeceği
  sınıf; güvenilmeyen bytecode için adım bütçesi Faz 2 işi.
- **Çağrı derinliği sınırı (güncelleme):** tree-walker artık VM'in aynen
  256 varsayılanını uygular; iki backend de yapılandırılabilir —
  `Interpreter(max_call_depth=...)` / `run_module(..., max_call_depth=...)`
  veya CLI `--max-call-depth N`. Hata konumludur:
  `Runtime Error at line L:C — call depth limit exceeded (N)`.

---

## 9. `@raw`'ın yeri

- **Faz 1:** düz `RawWord`; kaynak kısıtı checker'da (ilkel/stack), runtime'da
  yalnız değer taşır. Bugünkü davranış korunur.
- **Faz 2:** `RawWord` = adres + provenance etiketi. Geçerli kaynaklar:
  `@stack` frame tamponu, ayrılmış ham buffer. GC nesnesine ham erişim yok.
  Pointer aritmetiği burada anlam kazanır; aliasing denetimi `@raw`'ın
  sözleşmesidir, GC'nin değil.

Bu, `@raw`'ın v1'de "stack-pozisyon doğrulayıcı" rolünün neden dar olduğunu
ve gerçek evinin Faz 2 olduğunu netleştirir.

---

## 10. Uygulama sırası (iskelet sonrası)

1. Bu belgeyi opcode tablosu + encoding detayına genişlet (spec).
   **v0.1 yazıldı:** `NEX_BYTECODE_SPEC.md`.
2. AST → bytecode derleyici (alt küme: int, print, if/while, fn çağrısı).
3. VM döngüsü + Frame + host arayüzü (Faz 1).
   **Durum:** 2-3 v0.1 prototipinde tamam (control-flow alt kümesi).
4. Mod/borrow opcode'ları + mevcut own/stack süitlerinin parity'si.
   **Durum:** `@stack` ilk alt küme tamam. `@own` **Phase A** tamam
   (OWN_ALLOC/MOVE/FREE, frame sonu kill, verifier OWN yasağı, parity).
   Sırada: `&`/`&mut` borrow — `(frame, slot, epoch)` UAF takibi,
   BORROW_END kapsam çıkışı; sonra `@own`/borrow parity matrisi.
5. Differential harness + CI matrisi.
6. Ancak bundan sonra: register machine / GC ailesi / Rust port
   alternatiflerinin karşılaştırması (bu iskeletin içinde).

---

## 11. Verilmiş kararların özeti

| # | Karar | Durum |
|---|---|---|
| 1 | İki fazlı barınma: Python-hosted → Rust | **kilit** |
| 2 | Stack machine + açık frame | kilit (geri dönüşü olan) |
| 3 | Mod = slot etiketi (GC/OWN/STACK/RAW/BORROW) | **kilit** |
| 4 | Borrow = (frame, slot, epoch); NLL compiler-side | **kilit** |
| 5 | Faz 1 GC = host; Faz 2 = non-moving mark-sweep | yön kilit, detay açık |
| 6 | 1B opcode + u32 operand | v1 için kilit, optimizasyon açık |
| 7 | Parity testleri VM kabul kapısı | **kilit** |
| 8 | `@raw` gerçek adres = Faz 2 | **kilit** |

**Açık kalanlar:** GC detayları (mark-sweep vs. başka), compact encoding,
register machine'e geçiş ölçütü, generic/monster temsili, modül sistemi.
