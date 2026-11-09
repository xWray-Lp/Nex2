# Nex Bytecode Spec (v0.1 — tasarım)

> `NEX_VM_DESIGN.md`'nin 7. bölümünün ayrıntılandırılmış hâli. Bu bir
> **uygulama sözleşmesi taslağıdır**; prototip yazılmadan önce kilitlenmesi
> gereken encoding/opcode kararlarını tek yerde toplar. Semantik kaynak:
> `NEX_MEMORY_MODEL.md` + `NEX_VM_DESIGN.md`. Çelişki varsa önce o belgeler
> güncellenir, sonra burası.

---

## 1. Genel kurallar

- **Byte order:** little-endian.
- **Hizalama:** tüm bölümler 4 bayt hizalı.
- **Opcode:** 1 bayt. Operandlar sabit genişlikte `u32` (v1 basitlik;
  compact encoding ayrı optimizasyon turu).
- **İsim uzayı:** string'ler, fonksiyonlar, native'ler ve tipler sabit
  havuzundaki indekslerle anılır; bytecode içinde çıplak pointer yok.
- **Sürüm:** `version = 1`. Opcode/encoding değişirse artırılır; bilinmeyen
  opcode veya sürüm → verifier hatası, program çalışmaz.
- **Değerler ISA'da etiketsizdir.** Tip bilgisi verifier'ın soyut yorumu ve
  slot mod tablosundan gelir; çalışma zamanı yalnız mod kurallarını denetler.

---

## 2. Modül yerleşimi

Sabit 52 baytlık başlık, ardından bölümler:

```
offset  size  alan
0       4     magic = "NEXB"
4       2     version (=1)
6       2     flags (rezerve, 0)
8       4     entry_func (main fonksiyon indeksi; yoksa 0xFFFFFFFF)
12      4     const_count
16      4     const_off
20      4     func_count
24      4     func_off
28      4     native_count
32      4     native_off
36      4     code_len
40      4     code_off
44      4     debug_len
48      4     debug_off
```

Tüm `*_off` değerleri modül başına göre **mutlak** bayt ofsetidir.
Bölümler sırayla yazılır: const pool → func table → native table → code →
debug.

---

## 3. Sabit havuzu

```
Const {
  tag: u8
  payload
}

tag 0x01 INT      : i64 (8 bayt)
tag 0x02 FLOAT    : f64 (8 bayt)
tag 0x03 STR      : u32 len + UTF-8 bytes
tag 0x04 FUNC_REF : u32 func_idx
tag 0x05 NATIVE_REF: u32 native_idx
tag 0x06 TYPE     : u8 kind + u32 size     // @stack eleman/boyut tanımı
tag 0x07 NAME     : STR ile aynı; sembol/debug adı (opsiyonel ayrım)
```

`TYPE.kind`: `0x01 INT`, `0x02 FLOAT`, `0x03 BYTE`, `0x04 BOOL`, `0x05 PTR`.

---

## 4. Fonksiyon tablosu

```
FuncInfo {
  name_idx:       u32   // CONST STR indeksi
  arity:          u32
  n_locals:       u32
  max_stack:      u32
  code_off:       u32   // mutlak
  code_len:       u32
  slot_modes_off: u32   // n_locals baytlık mod tablosu (mutlak)
  line_off:       u32   // debug bölümü içinde
  line_len:       u32
  flags:          u32   // bit0 = async (v1 rezerve), bit1 = entry
}
```

**Slot modları** (`n_locals` bayt):

| Değer | Mod | Anlam |
|---|---|---|
| 0x00 | GC | normal değer |
| 0x01 | OWN | `@own` sahipliği |
| 0x02 | STACK | `@stack` gömülü tampon |
| 0x03 | RAW | `@raw` kelime |
| 0x04 | BORROW | `&x` / `&mut x` |
| 0x05 | TEMP | derleyici geçici slot'u (GC gibi) |

---

## 5. Native tablosu

```
NativeInfo {
  name_idx:   u32
  argc:       u32        // 0xFFFFFFFF = değişken
  flags:      u32        // bit0 = retains_ref, bit1 = pure, bit2 = structural
}
```

- `retains_ref = 1` → native, aldığı referansı çağrı sonrası saklayabilir.
  Verifier bu native'e `CALL_PURE_NATIVE` ile BORROW argüman geçirilmesini
  reddeder; `CALL_NATIVE` yolu checker'ın sınır kuralına tabidir.
- `len`/`length`/`keys`/`values`: `retains_ref = 0, pure = 1, structural = 1`.
  Checker'daki `PURE_BORROW_BUILTINS` ile **aynı liste** olmak zorunda;
  drift testi (`test_own.py` §13) bunu korur.

---

## 6. Opcode tablosu

Gösterim: `AD ` = operand, `(a b -- c)` = operand stack etkisi.

### 6.1 Yığın ve slotlar

| Hex | Mnemonic | Operandlar | Etki | Not |
|---|---|---|---|---|
| 0x00 | `NOP` | — | — | hizalama/debug |
| 0x01 | `CONST` | `u32 ci` | `( -- v)` | `K[ci]` |
| 0x02 | `LOAD_LOCAL` | `u32 si` | `( -- v)` | slot modu GC/TEMP/RAW/STACK okunabilir; OWN okunamaz (v1) |
| 0x03 | `STORE_LOCAL` | `u32 si` | `(v -- )` | OWN'a yalnız `OWN_ALLOC` yazar |
| 0x04 | `LOAD_GLOBAL` | `u32 ni` | `( -- v)` | |
| 0x05 | `STORE_GLOBAL` | `u32 ni` | `(v -- )` | |
| 0x06 | `POP` | — | `(v -- )` | |
| 0x07 | `DUP` | — | `(v -- v v)` | |
| 0x08 | `SWAP` | — | `(a b -- b a)` | |

### 6.2 Aritmetik / karşılaştırma

| Hex | Mnemonic | Etki |
|---|---|---|
| 0x10 | `ADD` | `(a b -- a+b)` |
| 0x11 | `SUB` | `(a b -- a-b)` |
| 0x12 | `MUL` | `(a b -- a*b)` |
| 0x13 | `DIV` | `(a b -- a/b)` (sıfır → trap) |
| 0x14 | `MOD` | `(a b -- a%b)` |
| 0x15 | `NEG` | `(a -- -a)` |
| 0x18 | `EQ` | `(a b -- bool)` |
| 0x19 | `NE` | `(a b -- bool)` |
| 0x1A | `LT` | `(a b -- bool)` |
| 0x1B | `LE` | `(a b -- bool)` |
| 0x1C | `GT` | `(a b -- bool)` |
| 0x1D | `GE` | `(a b -- bool)` |
| 0x1E | `NOT` | `(a -- bool)` |

`and`/`or` kısa devre gerektirdiği için **opcode değildir**; `JUMP_IF_*`
ile derlenir.

### 6.3 Kontrol akışı

| Hex | Mnemonic | Operandlar | Etki | Not |
|---|---|---|---|---|
| 0x20 | `JUMP` | `u32 target` | — | mutlak kod ofseti |
| 0x21 | `JUMP_IF_FALSE` | `u32 target` | `(cond -- )` | |
| 0x22 | `JUMP_IF_TRUE` | `u32 target` | `(cond -- )` | |
| 0x23 | `TRAP` | `u32 msg_idx` | — | konumlu runtime hatası |

### 6.4 Çağrı

| Hex | Mnemonic | Operandlar | Etki | Not |
|---|---|---|---|---|
| 0x28 | `CALL` | `u32 fi` | `(args -- ret)` | arity `FuncInfo`'dan |
| 0x29 | `CALL_NATIVE` | `u32 ni` | `(args -- ret)` | `retains_ref` olabilir |
| 0x2A | `CALL_PURE_NATIVE` | `u32 ni` | `(args -- ret)` | hedef `pure=1` olmalı |
| 0x2B | `RET` | — | `(ret? -- )` | own/borrow temizliği frame'de |

**Çağrı sınırı kuralı:** `CALL` ve `CALL_NATIVE` argümanlarında OWN/BORROW
modu varsa runtime hatası (v1: sahiplik/borç fonksiyon sınırını geçemez).
`CALL_PURE_NATIVE` BORROW kabul eder (referansı saklamaz).

### 6.5 Nesneler

| Hex | Mnemonic | Operandlar | Etki |
|---|---|---|---|
| 0x30 | `NEW_LIST` | `u32 n` | `(v1..vn -- list)` |
| 0x31 | `NEW_DICT` | `u32 n` | `(k1 v1..kn vn -- dict)` |
| 0x32 | `NEW_TUPLE` | `u32 n` | `(v1..vn -- tuple)` |
| 0x33 | `GET_INDEX` | — | `(obj idx -- v)` |
| 0x34 | `SET_INDEX` | — | `(obj idx v -- )` |
| 0x35 | `GET_MEMBER` | `u32 ni` | `(obj -- v)` |
| 0x36 | `SET_MEMBER` | `u32 ni` | `(obj v -- )` |

BORROW modlu bir değer `GET_INDEX`/`GET_MEMBER` operandı olabilir (per-access
çözümleme); heap konteynerine **yazılamaz** (runtime `_conv` karşılığı).

### 6.6 Mod opcode'ları

| Hex | Mnemonic | Operandlar | Etki | Not |
|---|---|---|---|---|
| 0x40 | `OWN_ALLOC` | `u32 si` | `(v -- )` | derin kopya + OWN slot |
| 0x41 | `OWN_MOVE` | `u32 si` | `( -- v)` | `to_gc`; borrow varsa hata, slot moved |
| 0x42 | `OWN_FREE` | `u32 si` | `( -- )` | `free`; borrow varsa hata, slot moved |
| 0x43 | `STACK_ALLOC` | `u32 si, u32 ti, u32 n` | `( -- )` | TYPE const + eleman sayısı |
| 0x44 | `STACK_MATERIALIZE` | `u32 si` | `( -- list)` | kaçışta heap kopyası |
| 0x45 | `RAW_MAKE` | `u32 si` | `(v -- )` | yalnız ilkel/stack kaynak (checker) |

### 6.7 Borrow opcode'ları

| Hex | Mnemonic | Operandlar | Etki | Not |
|---|---|---|---|---|
| 0x50 | `BORROW_SHARED` | `u32 dst, u32 src` | `( -- )` | src OWN olmalı; paylaşımlı sayaç++ |
| 0x51 | `BORROW_MUT` | `u32 dst, u32 src` | `( -- )` | dışlayıcı; çakışmada hata |
| 0x52 | `BORROW_END` | `u32 si` | `( -- )` | erken bırakma / blok çıkışı |
| 0x53 | `BORROW_CHECK` | `u32 si` | `( -- )` | released/epoch denetimi (debug) |

`BorrowSlot` çalışma zamanı temsili: `{target_frame, target_slot, epoch,
mutable, released}`. NLL geldiğinde derleyici yalnız `BORROW_END`'in
konumunu değiştirir — **yeni opcode yok**.

### 6.8 Kapsam / temizlik

| Hex | Mnemonic | Etki | Not |
|---|---|---|---|
| 0x58 | `BLOCK_ENTER` | cleanup kapsamı açar | |
| 0x59 | `BLOCK_EXIT` | kapsamı ters sırayla boşaltır | borrow release → own free → stack kill |

---

## 7. Verifier kuralları

Bytecode yüklenirken zorunlu; ihlal = program çalışmaz (runtime'a düşmez).

1. **Yığın:** her komut sınırında `0 <= depth <= max_stack`; `RET`/`BLOCK_EXIT`
   sonrası beklenen derinlik.
2. **Jump:** hedef kod aralığı içinde ve **komut başlangıcı**; operand
   ortasına atlama yasak. Fonksiyon sonundan taşma yasak (her yol `RET`).
3. **Slot:** `si < n_locals`; opcode ↔ slot modu uyumu (`OWN_MOVE` yalnız OWN,
   `BORROW_*` src OWN / dst BORROW, `RAW_MAKE` yalnız RAW, …).
4. **Borrow:** `BORROW_END` aktif ve lexical olarak iç içe bir borrow'a
   denk gelmeli; `RET` anında açık borrow kalmamalı.
5. **Native:** `CALL_PURE_NATIVE` hedefi `pure=1 && retains_ref=0`;
   `CALL_NATIVE` serbest.
6. **Kapsam:** `BLOCK_ENTER`/`BLOCK_EXIT` dengeli.
7. **Sabitler:** indeksler aralıkta; `FUNC_REF`/`NATIVE_REF` geçerli.

Tip doğrulaması v1 verifier'ın kapsamında değil (checker zaten yapıyor);
verifier mod/yığın/akış güvenliğinden sorumlu.

---

## 8. Debug / line tablosu

Fonksiyon başına:

```
u32 count
count × { u32 pc_off, u32 line, u32 col }
```

`pc_off` artan sırada; runtime hata anında ikili arama ile `line:col`
bulur. Hata mesajı biçimi tree-walker ile **aynı** olmalı
(`Runtime Error at line L:C — mesaj`) ki differential test metin
karşılaştırması yapabilsin.

---

## 9. Encoding örnekleri

`let x = 1 + 2` (slot 0, K[0]=1, K[1]=2):

```
01 00 00 00 00     CONST 0
01 01 00 00 00     CONST 1
10                 ADD
03 00 00 00 00     STORE_LOCAL 0
```

`&xs` bir ifade olarak `BORROW_SHARED dst,tmp, src=xs` şeklinde derlenir;
`len` çağrısı `CALL_PURE_NATIVE ni=len` olur (ni = NativeInfo indeksi):

```
50 01 00 00 00 00 00 00 00   BORROW_SHARED dst=1, src=0
2A 00 00 00 00               CALL_PURE_NATIVE 0   ; len
52 01 00 00 00               BORROW_END 1
```

> `CALL_PURE_NATIVE` operandı doğrudan `NativeInfo` indeksidir; isim
> yalnız debug bölümünde yaşar.

---

## 10. Disassembler biçimi (araç sözleşmesi)

```
; fn main  arity=0 locals=3 max_stack=8
0000  CONST        K0        ; 1
0005  CONST        K1        ; 2
0009  ADD
000A  STORE_LOCAL  0
000E  CALL_NATIVE  print(0)  ; argc=1
0012  RET
```

- `--disasm` çıktısı deterministik olmalı (test karşılaştırması için).
- Her satır: `pc`, mnemonic, operand, varsa `;` yorumu (satır numarası,
  sabit değeri, hedef etiketi).

---

## 11. Sürümleme ve uyumluluk

- `version` yalnız **kırıcı** encoding/opcode değişiminde artar.
- Yeni opcode eklemek eski programları bozmaz (bilinmeyen opcode zaten
  çalışmaz; eski modüller yeni VM'de çalışır).
- `flags` bitleri rezerve; bilinmeyen bit → verifier hatası.
- Native tablosu `retains_ref` bayrağı **ABI'nin parçası**; bayrak
  değişikliği checker allowlist'iyle birlikte güncellenmeli.

---

## 12. Açık uçlar (bilinçli)

- Compact operand encoding (u8/u16) — optimizasyon turu.
- Constant pool dedupe / string interning — optimizasyon turu.
- `BLOCK_EXIT` cleanup kodlaması (bytecode'da mı, frame metadata'da mı) —
  prototipte ölçülüp kararlaştırılacak; semantik aynı.
- Exception/trap tablosu (v1'de `TRAP` yeterli; `catch` VM dönemi işi).
- Register machine eşlemesi — bu ISA'nın üstünde ayrı tur.
