# Nex Sınıflar — VM Tasarım Notu (v0.1)

> Kısa karar notu. Amaç: `examples/tree_own.nex` dogfood'unu (sınıf tabanlı ağaç)
> VM'de byte-equal parity ile çalıştırmak. Üslup/kapsam için bkz.
> `NEX_INT_SEMANTICS.md`, `NEX_MEMORY_MODEL.md`; yapı ve durum takibi için
> `NEX_VM_DESIGN.md` §10–11. Bu belge **tasarım**dır; uygulama, kod ve commit
> içermez.

## 0. Bağlam (mevcut durum)

VM (tamamı yeşil, 381 test, 7/8 örnek byte-equal): ilkel tipler, list/dict,
`@stack`, `@own`+borrow (A/B), `@raw` (C), top-level `<script>`, `for`,
`println`/f-string. Desteklenmeyen dogfood: yalnız `examples/tree_own.nex`
(`class TreeNode` — tree-walker'da birinci sınıf, VM'de yok). Sınıflar
`NEX_VM_DESIGN.md` kapsam dışı listesinde.

## 1. Problem — tree-walker semantiği (referans)

- **Nesne modeli:** `NexClass(decl, closure)` → `call` ile `NexInstance(klass)`
  üretir; `NexInstance` alanları `fields: dict`, metodları `methods: dict`
  (değer: `Function`) tutar. `get_attr`: **önce alan, sonra metot** (metot
  erişimi `BoundMethod(instance, fn)` döndürür); `set_attr`: alan sözlüğüne yazar.
- **Init protokolü:** örnekleme sırasında (1) her `FieldDecl.default` varsa
  `interpreter.evaluate(default)` ile o anki bağlamda değerlendirilip alanlara
  yazılır, yoksa `None`; (2) `decl.methods` `Function` olarak `methods`'a
  konur; (3) `init` veya `__init__` varsa `[instance] + args` ile çağrılır.
  `Function.call`, ilk argüman `NexInstance` ve parametre sayısı `decl.params+1`
  ise `self`'i örtük bağlar (constructor ve `BoundMethod.call` bu sözleşmeye
  dayanır).
- **Forward class ref:** tree-walker `Program`'ı iki geçişte yürütür (önce tüm
  `ClassDecl`, sonra kalanlar) — tanımdan önce kullanım runtime'da tutarlı.
  Derleyici bugün `FnDecl`'leri benzer şekilde ön-indeksler.
- **Dogfood gereksinimi:** `TreeNode` alanları (`value/label/children`), `init`,
  `add_child` (alan mutasyonu), `total` (`self` üzerinden özyineleme, `for` ile
  çocuk gezimi), GC referans paylaşımı ve `@own` snapshot etkileşimi (§3.6).

## 2. Kapsam dışı (bilinçli, v1)

- Kalıtım/`super`, abstract/interface, dataclass/destructure, dunder/metaprogramming.
- Fonksiyon içinde sınıf tanımı.
- Dinamik alan ekleme/silme kararı §6(c)'de; statik tipli "açık şema" yok.
- Alan varsayılanları: tree-walker örnekleme anında **keyfi ifade** değerlendirir.
  **v1 önerisi:** yalnız basit literal varsayılanlar (`int/float/str/bool/null`);
  karmaşık ifadeler v1'de reddedilir (checker/derleyici), tam parity sonraya
  bırakılır (tip-annotated explicit defaults'ın örnekleme-anı semantiği korunur).

## 3. Önerilen kararlar

### 3.1 Yerleşim (layout)
- **Karar:** host `NexInstance { klass_index, fields: dict|array }` ve
  `BoundMethod { receiver: Handle, fn_index }` nesneleri. **Dict-shim yok.**
- **Gerekçe:** shim init sırasını, bound `self` bağlamasını ve dinamik-alan
  semantiğini bozar; `@own`/deepcopy ve member lookup kuralları (§3.5) ile
  uyumsuz sapmalar üretir. Değer temsili `NEX_VM_DESIGN.md` §3 `Instance(Handle)`
  / `Function(Handle)` birleşimine oturur.

### 3.2 Kodlama (encoding)
- Yeni opcode grubu: `NEW_INSTANCE u32 cls_const` (sabit havuzunda sınıf ref), **hex `0x37`**.
- Yeni sabit havuzu etiketi: **`CLASS_REF = 0x0A`** (const bir `ClassRef` v2'de kullanılır).
- Ayrılmış `GET_MEMBER 0x35` / `SET_MEMBER 0x36` yeniden kullanılır; operand:
  `u32 member_name_const` (string). Stack etkisi: `GET_MEMBER (obj -> val)`,
  `SET_MEMBER (obj, val ->)`. **`GET_MEMBER` ve `SET_MEMBER` şu anda `UNIMPLEMENTED_OPCODES` içindedir ve açılacaktır.**
- `CALL_BOUND u32 argc`: yığın üstü `BoundMethod`, ardından `argc` argüman;
  çağrı protokolü tree-walker ile aynı — `fn(receiver, args...)` (receiver
  otomatik ilk argüman). `CALL`/native yolları mevcut sözleşmesini korur.

### 3.3 ClassInfo ve modül sürümü
- **Header sürüm geçidi (seçenek ii):** v2 header **60 bayt** olarak genişletilir (v1 52 baytlık layout'tan farklı). Genişletilmiş header'a **`class_count` (u32) + `class_off` (u32)** eklenir. Decoder, header sürümüne göre dal alır (**v1 → 52-byte layout**, **v2 → 60-byte layout**); **v1 modülleri tam olarak desteklenmeye devam eder**.
- **Bölüm yerleşimi:** v2'de `ClassTable` **natives** bölümünden sonra, **slot_modes** bölümünden önce yer alır (byte layout bu sıraya göre belgelenir). Daha önceki belirsiz "natives sonrası yeni bölüm" ifadesinin yerini bu net sıra alır.
- **İçerik:** `ClassTable { count, [ClassInfo] }`. `ClassInfo { name_idx, field_count, field_name_idx[], method_count, [ (method_name_idx, fn_const_idx) ] }` (metot gövdesi `FuncInfo`/FUNC_REF ile).
- **Zorunluluk:** **v2'de `ClassTable` zorunludur (boş olabilir)**. Sınıf eklenmesi mevcut (const/func/code/debug) bölümlerin anlamını değiştirmez.
- **Offset'ler:** v2'de mutlak offset'ler (`code_off`, `debug_off`, fonksiyon başına `slot_modes_off`, `code_off`) eklenen `ClassTable`'ı atlayacak şekilde yeniden hesaplanmalıdır (`encode_module` bunları bugün hesaplar; v2 de aynı şekilde yeniden hesaplamalıdır — aksi halde bölümler çakışır).
- **Doğrulama:** Verifier, v1 kurallarının geri kalanına uygulanmaya devam eder; **v2 için sınıf tablosu doğrulaması (isim indislerinin aralık içinde olması) yenidir**.

### 3.4 Derleyici
- İki geçiş: (1) `FnDecl` ve `ClassDecl` adlarını/slotlarını ön-indeksle
  (forward ref — tree-walker `Program` geçişiyle parity); (2) gövdeleri derle.
- Örnekleme: `Foo(args...)` → `NEW_INSTANCE <Foo>` + `init`/`__init__` için
  `GET_MEMBER`/`CALL` (veya bound çağrı) zinciri. **Öneri:** derleyici, tanımlı
  `init`/`__init__`'ten çağrılabilir bir giriş üretir (gerekirse dahili
  `__ctor__` wrapper).
- **`init`/`__init__` yoksa semantik:** sınıf **init olmadan örneklenebilir**. `NexClass.call` argc 0 gerektirmez; **çağrı argümanları yine de değerlendirilir (yan etkiler)** ve **göz ardı edilir** (interpreter, çağrı sitesinde argümanları değerlendirir ve bırakır; `A(1,2,3)` init yokken başarılıdır). VM planı da aynıdır — init yokken **çağrı argümanları derlenip (yan etkiler), atılır ve örnekleme başarılıdır**. `self` ilk parametre olarak değil, alıcı-bağlama yoluyla gelir (tree-walker ile aynı).
- Metot derlemesi: serbest fonksiyon gibi `FuncInfo`; ilk örtük argüman `self`.

### 3.4a Üye çağrıları/atamaları için lowerings (GET_MEMBER tek başına yetersiz)
`GET_MEMBER`/`SET_MEMBER` temel işlemler olmakla birlikte, üye erişimlerinin bağlamına göre aşağıdaki lowering kuralları uygulanır:

- **Üye OKUMA (çağrı olmayan bağlam):** `let x = obj.field` → `obj` değerlendir, **`GET_MEMBER`** (alan önce, metot sonra; metot okuması `BoundMethod` üretir).
- **Üye ATAMA:** `obj.field = v` → **`obj` değerlendir**, **`v` değerlendir**, ardından **`SET_MEMBER`** (hedef `INSTANCE` olmalı; izinli `&mut` borrow durumları §3.5'e tabidir).
- **Üye ÇAĞRISI:** `obj.method(args...)` → **`obj` değerlendir**, **`GET_MEMBER`** (yığında `BoundMethod` üretir), **argümanlar değerlendir**, ardından **`CALL_BOUND argc`**. Çağrı protokolü tree-walker ile aynı olup receiver otomatik ilk argüman olarak geçirilir.
- **Özel durum:** `push()` gibi liste metotları native kalır ve şu anda **`_CALL_PURE_NATIVE push` özel durumu** olarak ele alınır; bu, üye okuma/yazma kurallarıyla aynı çerçevede değerlendirilir (`GET/SET_MEMBER` ile tutarlılık korunur).
- **Bileşim (composition):** üye operasyonları diğer tüm yapılarla bileşir — örn. `self.children.push(c)`: `GET_MEMBER(children)` → `push` native özel durumu; `for c in self.children`: üye okuması mevcut liste-iterasyon lowering'ine beslenir; `to_own([left.value, right.value])`: bir liste literal'i içindeki iç içe üye okumaları.

### 3.5 Metot/alan arama ve @own/borrow etkileşimi
- **Arama kuralı:** alan önce, metot sonra `BoundMethod` (tree-walker ile aynı).
  `init`/`__init__` örneklemeden hemen sonra otomatik çağrılır.
- **Saklama/deepcopy:** `OwnValue`'nun alana yazımı `_nex_deepcopy` kuralı gibi
  VM hatasıdır ("cannot deepcopy an @own value"); borrow değerleri v1'de
  **blok-yerel**dir ve alanlara saklanamaz. `to_own(instance)` alanları deep-copy
  eder, metot tablosunu (sınıf kimliği/fn referansları) paylaşır (tree-walker
  davranışı). `BoundMethod` saklanamaz/yazdırılamaz (own ile aynı kısıt ailesi).
- **SET_MEMBER hedefi:** alıcı `INSTANCE` olmalı; `BORROW(&mut)` üzerinden
  yazım izinliyse yalnız geçerli/mutable borrow ve hedef instance için geçerli
  olur (ayrıntı verifier+runtime kuralı, §4).
- **VM `_deep_copy`:** `NexInstance` durumu için **instance durumu eklenmelidir** — alanlar **deep-copy** edilir, metotlar **paylaşılır** (tree-walker davranışıyla aynı).

### 3.6 Hata parity'si
Konumlu hatalar tree-walker ile eşleşir: yanlış argüman sayısıyla örnekleme,
bilinmeyen üye okuma, bilinmeyen üyeyi çağırma, **`init` yokken argüman verilmesi artık hata değildir** (tree-walker: init yoksa sınıf yine de örneklenir — argümanlar değerlendirilip göz ardı edilir; `A(1,2,3)` init yokken başarılıdır; VM'de aynı semantik uygulanır), alan
varsayılanı değerlendirme sırası (bildirim sırası), `self` bağlama hataları.

## 4. Opcode / bölüm / doğrulama özeti

| Öğe | Biçim / kural | Not |
|---|---|---|
| `NEW_INSTANCE` | `u32 cls_const` → `Instance` (**0x37**) | const bir `CLASS_REF` (`0x0A`) (v2) olmalı |
| `GET_MEMBER` (0x35) | `(obj)`, `u32 name_idx` → `val` | alıcı `INSTANCE`; alan→değer, metot→`BoundMethod`; yoksa konumlu hata. **Şu anda `UNIMPLEMENTED_OPCODES` — açılacak.** |
| `SET_MEMBER` (0x36) | `(obj,val)`, `u32 name_idx` → `()` | alıcı `INSTANCE` (veya izinli `&mut` borrow); OwnValue/borrow saklama yasağı §3.5. **Şu anda `UNIMPLEMENTED_OPCODES` — açılacak.** |
| `CALL_BOUND` | `(bound, a1..an)`, `u32 argc` → `ret` | alıcı `BOUND_METHOD`; çağrı derinliği sınırı mevcut kuralla |
| `ClassTable` | `name, fields[], methods[(name,fn_ref)]` | v2, **natives** sonrası **slot_modes** öncesi; **v2'de zorunlu (boş olabilir)** |
| Verifier | stack yüksekliği, const türü, jump | `NEW_INSTANCE` hedefi sınıf; member const string; `CALL_BOUND` argc uyumu (statik biliniyorsa). **v2'de ek olarak sınıf tablosu doğrulaması (isim indislerinin aralık içinde olması) uygulanır**; v1 kurallarının geri kalanı geçerliliğini korur. |

## 5. Parity planı

- **Kabul:** `examples/tree_own.nex` VM backend'inde **byte-equal** stdout
  (tree-walker referans). `NEX_VM_DESIGN.md` §8 differential harness'e dahil.
- **Test matrisi:** init çağrısı ve argümanlar; **`Class(arg)` init yokken başarılı ve argümanların yan etkileri değerlendirilip atılır** (test matrisi maddesi eklendi); metot çağrısı ve `self`
  özyineleme (`total`); alan mutasyonu (`add_child`); iç içe list alanı
  (`children`); `@own` snapshot ile instance içeriği (değer kopyası/izolasyon);
  alan varsayılanı bildirim sırası; forward class ref (sınıf/kurucu tanımdan
  önce referans — `test_stack` dogfood benzeri düzen).

### Enforcement checklist
Uygulama için aşağıdaki dosyalar muhtemel değişiklik noktalarıdır:

- **`bytecode.py`** — `NEW_INSTANCE` hex (`0x37`), `CLASS_REF = 0x0A`, `GET_MEMBER`/`SET_MEMBER` opcode tanımları; `UNIMPLEMENTED_OPCODES` listesinden çıkarma; v2 header (60 bayt, `class_count`/`class_off`), v1 (52 bayt) dalı ve `ClassTable` bölümü (natives → slot_modes arası) okuma/yazma.
- **`compiler.py`** — **`ClassDecl`** için ön-indeksleme eklenmeli (iki geçiş, forward ref parity); `_func_index` hâlihazırda `FnDecl`'leri ön-indeksler, yalnız `ClassDecl`/sınıf üyeleri desteklenmez. Örnekleme loweringi (`NEW_INSTANCE` + init/member zinciri); üye atama/okuma/çağrı loweringi (§3.4a).
- **`vm.py`** — `NEW_INSTANCE`/`GET_MEMBER`/`SET_MEMBER`/`CALL_BOUND` runtime'ı; init yokken argüman değerlendirme+atım semantiği; üye arama (alan→metot, `BoundMethod`); `@own`/borrow kuralları; **`_deep_copy`'ye instance durumu eklenmeli** (alanlar deep-copy, metotlar paylaşılır).
- **`check_vm_parity.py`** — sınıf/üye çağrıları için parity testleri ve hata mesajı eşleştirmesi (init yokken argüman kabulü dahil).
- **spec docs** — `NEX_VM_DESIGN.md` vb. ile header byte layout (v1 52B / v2 60B), bölüm sırası (natives → ClassTable → slot_modes) ve const tagleri güncellenmeli.

## 6. Açık sorular (kullanıcı onayı gerekir)

- **(a) Sürüm:** modül `version 1→2` ve **zorunlu** `ClassTable` (boş dahi olsa) mı? (öneri: 1→2, net gate; v1 tam geriye uyumlu okunur.)
- **(b) İsim:** `init` ve `__init__` ikilisi korunacak mı (her ikisi de kabul,
  tek canonical hangisi)? (öneri: ikisini de tanı, tek iniş noktası.)
- **(c) Dinamik alan:** `obj.newfield = x` tree-walker'da serbest; v1'de
  **reddet** (bildirilmiş alan şeması) mi, yoksa koru mu? (öneri: reddet —
  statik/VM doğrulanabilirlik.)
- **(d) Dict-benzeri yüzey:** instance üzerinde kullanıcı kodundan `.fields`
  / dict operasyonları (keys/iteration) açık API olacak mı? (öneri: hayır;
  üye erişimi yalnız `.`/GET-SET_MEMBER.)

> Bu sorular kapatılmadan §3 kararları "öneri" statüsündedir; parity hedefi §5
> değişmez.
