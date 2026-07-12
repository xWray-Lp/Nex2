"""
Nex Language — Parser
Token listesini AST'ye dönüştürür.
Recursive Descent Parser.
"""

from typing import Optional

from ast_nodes import *
from lexer import TT, Lexer, Token

# ─── Parser Hatası ────────────────────────────────────────────────────────────


class ParseError(Exception):
    def __init__(self, msg: str, tok: Token):
        super().__init__(
            f"[Parse Error] Line {tok.line}:{tok.col} — {msg} (got {tok!r})"
        )
        self.token = tok


# ─── Parser ───────────────────────────────────────────────────────────────────


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = [
            t for t in tokens if t.type not in (TT.NEWLINE,)
        ]  # NEWLINE ayıkla (özel durumlar hariç)
        # Not: INDENT/DEDENT korunur
        self.pos = 0
        self.recovering = False
        self.recovered_errors: list[ParseError] = []

    # ── Token yardımcıları ────────────────────────────────────────

    def peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return self.tokens[-1]  # EOF

    def check(self, *types: TT) -> bool:
        return self.peek().type in types

    def advance(self) -> Token:
        tok = self.peek()
        if tok.type != TT.EOF:
            self.pos += 1
        return tok

    def expect(self, tt: TT, msg: str = "") -> Token:
        if self.check(tt):
            return self.advance()
        tok = self.peek()
        raise ParseError(msg or f"Expected {tt.name}", tok)

    def match(self, *types: TT) -> Optional[Token]:
        if self.check(*types):
            return self.advance()
        return None

    def skip_newlines(self):
        while self.check(TT.NEWLINE):
            self.advance()

    def error(self, msg: str) -> ParseError:
        return ParseError(msg, self.peek())

    def synchronize(self, err: ParseError):
        """
        Basit panic-mode error recovery.
        Bir sonraki güvenli statement/toplevel başlangıcına kadar ilerler.
        """
        self.recovering = True
        self.recovered_errors.append(err)

        # Şu anki hatalı token'dan en az bir adım ilerle
        if not self.check(TT.EOF):
            self.advance()

        sync_tokens = {
            TT.LET,
            TT.CONST,
            TT.OWN,
            TT.RAW,
            TT.STACK,
            TT.FN,
            TT.ASYNC,
            TT.CLASS,
            TT.INTERFACE,
            TT.IF,
            TT.FOR,
            TT.WHILE,
            TT.MATCH,
            TT.TRY,
            TT.RETURN,
            TT.BREAK,
            TT.CONTINUE,
            TT.IMPORT,
            TT.MODULE,
            TT.EXPORT,
            TT.DEDENT,
            TT.EOF,
        }

        while not self.check(TT.EOF):
            if self.check(*sync_tokens):
                break
            self.advance()

        self.recovering = False

    # ── Giriş noktası ────────────────────────────────────────────

    def parse(self) -> Program:
        stmts = []
        while not self.check(TT.EOF):
            self.skip_newlines()
            if self.check(TT.EOF):
                break
            try:
                stmts.append(self.parse_toplevel())
            except ParseError as err:
                # Hata sonrası parser'ın tamamen durmaması için recovery
                self.synchronize(err)

        if self.recovered_errors:
            first = self.recovered_errors[0]
            if len(self.recovered_errors) == 1:
                raise first
            summary = "; ".join(str(e) for e in self.recovered_errors[:3])
            if len(self.recovered_errors) > 3:
                summary += f"; ... (+{len(self.recovered_errors) - 3} more)"
            raise ParseError(
                f"Recovered from {len(self.recovered_errors)} parse error(s): {summary}",
                first.token,
            )

        tok = self.peek()
        return Program(stmts, line=0, col=0)

    # ── Top-level ─────────────────────────────────────────────────

    def parse_toplevel(self) -> Node:
        tok = self.peek()

        if self.check(TT.MODULE):
            return self.parse_module()
        if self.check(TT.IMPORT):
            return self.parse_import()
        if self.check(TT.EXPORT):
            return self.parse_export()
        if self.check(TT.CLASS):
            return self.parse_class()
        if self.check(TT.INTERFACE):
            return self.parse_interface()
        if self.check(TT.ASYNC) or self.check(TT.FN):
            return self.parse_fn_decl()
        return self.parse_stmt()

    # ── Modül / Import ───────────────────────────────────────────

    def parse_module(self) -> ModuleStmt:
        tok = self.expect(TT.MODULE)
        path = self._parse_dotted_name()
        return ModuleStmt(path, line=tok.line, col=tok.col)

    def parse_import(self) -> ImportStmt:
        tok = self.expect(TT.IMPORT)

        # Parse path manually for import syntax
        path = [self.expect(TT.IDENT).value]

        # path.to.mod veya path.{A, B} ayrımı
        while self.check(TT.DOT) and self.peek(1).type != TT.LBRACE:
            self.advance()  # consume DOT
            path.append(self.expect(TT.IDENT).value)

        names = []
        alias = None

        # Seçici import: path.{name1, name2}
        if self.check(TT.DOT) and self.peek(1).type == TT.LBRACE:
            self.advance()  # consume DOT
            self.expect(TT.LBRACE)
            names.append(self.expect(TT.IDENT).value)
            while self.match(TT.COMMA):
                names.append(self.expect(TT.IDENT).value)
            self.expect(TT.RBRACE)

        if self.match(TT.AS):
            alias = self.expect(TT.IDENT).value
        return ImportStmt(path, names, alias, line=tok.line, col=tok.col)

    def parse_export(self) -> Node:
        self.advance()  # export
        node = self.parse_toplevel()
        if isinstance(node, FnDecl):
            node.is_export = True
        return node

    def _parse_dotted_name(self) -> list[str]:
        parts = [self.expect(TT.IDENT).value]
        while self.match(TT.DOT):
            parts.append(self.expect(TT.IDENT).value)
        return parts

    # ── Class ─────────────────────────────────────────────────────

    def parse_class(self) -> ClassDecl:
        tok = self.expect(TT.CLASS)
        name = self.expect(TT.IDENT).value
        type_params = self._parse_type_params()
        base = None
        if self.match(TT.EXTENDS):
            base = self.expect(TT.IDENT).value
        interfaces = []
        if self.match(TT.IMPLEMENTS):
            interfaces.append(self.expect(TT.IDENT).value)
            while self.match(TT.COMMA):
                interfaces.append(self.expect(TT.IDENT).value)
        self.expect(TT.COLON)
        self.expect(TT.INDENT)
        fields, methods = [], []
        while not self.check(TT.DEDENT) and not self.check(TT.EOF):
            self.skip_newlines()
            if self.check(TT.FN) or self.check(TT.ASYNC):
                methods.append(self.parse_fn_decl())
            elif self.check(TT.IDENT):
                fields.append(self._parse_field())
            else:
                break
        self.match(TT.DEDENT)
        return ClassDecl(
            name,
            type_params,
            base,
            interfaces,
            fields,
            methods,
            line=tok.line,
            col=tok.col,
        )

    def _parse_field(self) -> FieldDecl:
        tok = self.peek()
        name = self.expect(TT.IDENT).value
        self.expect(TT.COLON)
        type_ann = self.parse_type()
        default = None
        if self.match(TT.ASSIGN):
            default = self.parse_expr()
        return FieldDecl(name, type_ann, default, line=tok.line, col=tok.col)

    # ── Interface ─────────────────────────────────────────────────

    def parse_interface(self) -> InterfaceDecl:
        tok = self.expect(TT.INTERFACE)
        name = self.expect(TT.IDENT).value
        self.expect(TT.COLON)
        self.expect(TT.INDENT)
        methods = []
        while not self.check(TT.DEDENT) and not self.check(TT.EOF):
            self.skip_newlines()
            if self.check(TT.FN):
                methods.append(self.parse_fn_decl(body_optional=True))
            else:
                break
        self.match(TT.DEDENT)
        return InterfaceDecl(name, methods, line=tok.line, col=tok.col)

    # ── Fonksiyon ─────────────────────────────────────────────────

    def parse_fn_decl(self, body_optional: bool = False) -> FnDecl:
        tok = self.peek()
        is_async = bool(self.match(TT.ASYNC))
        self.expect(TT.FN)
        name = self.expect(TT.IDENT).value
        type_params = self._parse_type_params()
        self.expect(TT.LPAREN)
        params = self._parse_params()
        self.expect(TT.RPAREN)
        return_type = None
        if self.match(TT.ARROW):
            return_type = self.parse_type()
        body = None
        if self.check(TT.COLON):
            self.advance()
            body = self.parse_block()
        elif not body_optional:
            raise self.error("Expected ':' to begin function body")
        return FnDecl(
            name,
            type_params,
            params,
            return_type,
            body,
            is_async,
            line=tok.line,
            col=tok.col,
        )

    def _parse_type_params(self) -> list[str]:
        params = []
        if self.match(TT.LBRACKET):
            params.append(self.expect(TT.IDENT).value)
            while self.match(TT.COMMA):
                params.append(self.expect(TT.IDENT).value)
            self.expect(TT.RBRACKET)
        return params

    def _parse_params(self) -> list[Param]:
        params = []
        if self.check(TT.RPAREN):
            return params
        params.append(self._parse_param())
        while self.match(TT.COMMA):
            params.append(self._parse_param())
        return params

    def _parse_param(self) -> Param:
        tok = self.peek()
        is_ref = False
        is_mut_ref = False
        if self.match(TT.AMP):
            if self.match(TT.IDENT):  # "mut"
                is_mut_ref = True
            is_ref = True
        # self keyword'ü param olarak kabul et
        if self.check(TT.SELF):
            name = self.advance().value
        else:
            name = self.expect(TT.IDENT).value
        type_ann = None
        if self.match(TT.COLON):
            type_ann = self.parse_type()
        default = None
        if self.match(TT.ASSIGN):
            default = self.parse_expr()
        return Param(
            name, type_ann, default, is_ref, is_mut_ref, line=tok.line, col=tok.col
        )

    # ── Blok ──────────────────────────────────────────────────────

    def parse_block(self) -> Block:
        tok = self.peek()
        self.expect(TT.INDENT)
        stmts = []
        while not self.check(TT.DEDENT) and not self.check(TT.EOF):
            self.skip_newlines()
            if self.check(TT.DEDENT) or self.check(TT.EOF):
                break
            stmts.append(self.parse_stmt())
        self.match(TT.DEDENT)
        return Block(stmts, line=tok.line, col=tok.col)

    # ── Statements ────────────────────────────────────────────────

    def parse_stmt(self) -> Node:
        tok = self.peek()

        if self.check(TT.LET) or self.check(TT.CONST):
            return self.parse_var_decl()
        if self.check(TT.OWN) or self.check(TT.RAW) or self.check(TT.STACK):
            return self.parse_var_decl()
        if self.check(TT.FN) or self.check(TT.ASYNC):
            return self.parse_fn_decl()
        if self.check(TT.RETURN):
            return self.parse_return()
        if self.check(TT.IF):
            return self.parse_if()
        if self.check(TT.FOR):
            return self.parse_for()
        if self.check(TT.WHILE):
            return self.parse_while()
        if self.check(TT.MATCH):
            return self.parse_match_stmt()
        if self.check(TT.TRY):
            return self.parse_try()
        if self.check(TT.BREAK):
            self.advance()
            return BreakStmt(line=tok.line, col=tok.col)
        if self.check(TT.CONTINUE):
            self.advance()
            return ContinueStmt(line=tok.line, col=tok.col)

        # Expression statement (assignment dahil)
        expr = self.parse_expr()
        return ExprStmt(expr, line=tok.line, col=tok.col)

    def parse_var_decl(self) -> Node:
        tok = self.peek()
        mem_mode = None
        if self.check(TT.OWN):
            mem_mode = "own"
            self.advance()
        elif self.check(TT.RAW):
            mem_mode = "raw"
            self.advance()
        elif self.check(TT.STACK):
            mem_mode = "stack"
            self.advance()

        mutable = True
        if self.check(TT.LET):
            self.advance()
            mutable = True
        elif self.check(TT.CONST):
            self.advance()
            mutable = False

        # Çoklu atama: let a, b = 1, 2
        name = self.expect(TT.IDENT).value
        if self.match(TT.COMMA):
            names = [name]
            names.append(self.expect(TT.IDENT).value)
            while self.match(TT.COMMA):
                names.append(self.expect(TT.IDENT).value)
            self.expect(TT.ASSIGN)
            values = [self.parse_expr()]
            while self.match(TT.COMMA):
                values.append(self.parse_expr())
            return MultiVarDecl(names, values, mutable, line=tok.line, col=tok.col)

        type_ann = None
        if self.match(TT.COLON):
            type_ann = self.parse_type()
        value = None
        if self.match(TT.ASSIGN):
            value = self.parse_expr()
        return VarDecl(
            name, type_ann, value, mutable, mem_mode, line=tok.line, col=tok.col
        )

    def parse_return(self) -> ReturnStmt:
        tok = self.expect(TT.RETURN)
        value = None
        if (
            not self.check(TT.NEWLINE)
            and not self.check(TT.DEDENT)
            and not self.check(TT.EOF)
        ):
            value = self.parse_expr()
        return ReturnStmt(value, line=tok.line, col=tok.col)

    def parse_if(self) -> IfStmt:
        tok = self.expect(TT.IF)
        cond = self.parse_expr()
        self.expect(TT.COLON)
        then_block = self.parse_block()
        elif_arms = []
        else_block = None
        while self.check(TT.ELIF):
            self.advance()
            ec = self.parse_expr()
            self.expect(TT.COLON)
            eb = self.parse_block()
            elif_arms.append((ec, eb))
        if self.match(TT.ELSE):
            self.expect(TT.COLON)
            else_block = self.parse_block()
        return IfStmt(
            cond, then_block, elif_arms, else_block, line=tok.line, col=tok.col
        )

    def parse_for(self) -> ForStmt:
        tok = self.expect(TT.FOR)
        target = self._parse_for_target()
        self.expect(TT.IN)
        iterable = self.parse_expr()
        self.expect(TT.COLON)
        body = self.parse_block()
        return ForStmt(target, iterable, body, line=tok.line, col=tok.col)

    def _parse_for_target(self) -> Node:
        tok = self.peek()
        name = self.expect(TT.IDENT).value
        if self.match(TT.COMMA):
            names = [Ident(name, line=tok.line, col=tok.col)]
            names.append(
                Ident(
                    self.expect(TT.IDENT).value,
                    line=self.peek().line,
                    col=self.peek().col,
                )
            )
            while self.match(TT.COMMA):
                names.append(
                    Ident(
                        self.expect(TT.IDENT).value,
                        line=self.peek().line,
                        col=self.peek().col,
                    )
                )
            return TupleLit(names, line=tok.line, col=tok.col)
        return Ident(name, line=tok.line, col=tok.col)

    def parse_while(self) -> WhileStmt:
        tok = self.expect(TT.WHILE)
        cond = self.parse_expr()
        self.expect(TT.COLON)
        body = self.parse_block()
        return WhileStmt(cond, body, line=tok.line, col=tok.col)

    def parse_match_stmt(self) -> MatchStmt:
        tok = self.expect(TT.MATCH)
        subject = self.parse_expr()
        self.expect(TT.COLON)
        self.expect(TT.INDENT)
        arms = []
        while not self.check(TT.DEDENT) and not self.check(TT.EOF):
            self.skip_newlines()
            if self.check(TT.DEDENT):
                break
            arms.append(self._parse_match_arm())
        self.match(TT.DEDENT)
        return MatchStmt(subject, arms, line=tok.line, col=tok.col)

    def _parse_match_arm(self) -> MatchArm:
        tok = self.peek()
        pattern = self._parse_pattern()
        # optional guard: if <expr>
        guard = None
        if self.check(TT.IF):
            self.advance()
            guard = self.parse_expr()
        self.expect(TT.ARROW)
        if self.check(TT.INDENT):
            body = self.parse_block()
        elif self.check(TT.RETURN):
            body = self.parse_return()
        elif self.check(TT.BREAK):
            self.advance()
            body = BreakStmt(line=tok.line, col=tok.col)
        elif self.check(TT.CONTINUE):
            self.advance()
            body = ContinueStmt(line=tok.line, col=tok.col)
        else:
            body = self.parse_expr()
        return MatchArm(pattern, body, guard=guard, line=tok.line, col=tok.col)

    def _parse_pattern(self) -> Node:
        tok = self.peek()
        # case keyword'ünü atla
        if self.check(TT.IDENT) and self.peek().value == "case":
            self.advance()
        if self.check(TT.UNDERSCORE):
            self.advance()
            return Ident("_", line=tok.line, col=tok.col)
        if self.check(TT.INT):
            v = self.advance().value
            node = IntLit(v, line=tok.line, col=tok.col)
            # range pattern: 1..10
            if self.check(TT.DOTDOT):
                self.advance()
                end = IntLit(self.expect(TT.INT).value, line=tok.line, col=tok.col)
                return RangeExpr(node, end, line=tok.line, col=tok.col)
            return node
        if self.check(TT.FLOAT):
            v = self.advance().value
            return FloatLit(v, line=tok.line, col=tok.col)
        if self.check(TT.STRING):
            v = self.advance().value
            return StrLit(v, line=tok.line, col=tok.col)
        if self.check(TT.BOOL):
            v = self.advance().value
            return BoolLit(v, line=tok.line, col=tok.col)
        if self.check(TT.IDENT):
            name = self.advance().value
            if self.match(TT.LPAREN):
                inner = Ident(self.expect(TT.IDENT).value, line=tok.line, col=tok.col)
                self.expect(TT.RPAREN)
                return CallExpr(
                    Ident(name, line=tok.line, col=tok.col),
                    [inner],
                    line=tok.line,
                    col=tok.col,
                )
            return Ident(name, line=tok.line, col=tok.col)
        raise self.error("Expected pattern")

    def parse_try(self) -> TryCatchStmt:
        tok = self.expect(TT.TRY)
        self.expect(TT.COLON)
        try_block = self.parse_block()
        catch_arms = []
        finally_block = None
        while self.check(TT.CATCH):
            self.advance()
            exc_type, alias = None, None
            if self.check(TT.IDENT):
                first = self.advance().value
                if self.check(TT.IDENT):
                    # catch TypeError e: → exc_type=TypeError, alias=e
                    exc_type = first
                    alias = self.advance().value
                elif self.match(TT.AS):
                    # catch TypeError as e:
                    exc_type = first
                    alias = self.expect(TT.IDENT).value
                else:
                    # catch e: → alias=e, catch-all
                    alias = first
            self.expect(TT.COLON)
            body = self.parse_block()
            catch_arms.append(
                CatchArm(exc_type, alias, body, line=tok.line, col=tok.col)
            )
        if self.match(TT.FINALLY):
            self.expect(TT.COLON)
            finally_block = self.parse_block()
        return TryCatchStmt(
            try_block, catch_arms, finally_block, line=tok.line, col=tok.col
        )

    # ── Type Parser ───────────────────────────────────────────────

    def parse_type(self) -> Node:
        tok = self.peek()

        # Referans: &T, &mut T
        if self.match(TT.AMP):
            mutable = False
            if self.check(TT.IDENT) and self.peek().value == "mut":
                self.advance()
                mutable = True
            inner = self.parse_type()
            return TypeRef(inner, mutable, line=tok.line, col=tok.col)

        # Tuple: (int, str)
        if self.match(TT.LPAREN):
            elems = [self.parse_type()]
            while self.match(TT.COMMA):
                elems.append(self.parse_type())
            self.expect(TT.RPAREN)
            node = TypeTuple(elems, line=tok.line, col=tok.col)
            if self.match(TT.QUESTION):
                return TypeNullable(node, line=tok.line, col=tok.col)
            return node

        # Liste: [int]
        if self.match(TT.LBRACKET):
            inner = self.parse_type()
            self.expect(TT.RBRACKET)
            node = TypeGeneric("List", [inner], line=tok.line, col=tok.col)
            if self.match(TT.QUESTION):
                return TypeNullable(node, line=tok.line, col=tok.col)
            return node

        # Dict: {str: int}
        if self.match(TT.LBRACE):
            key = self.parse_type()
            self.expect(TT.COLON)
            val = self.parse_type()
            self.expect(TT.RBRACE)
            node = TypeGeneric("Dict", [key, val], line=tok.line, col=tok.col)
            if self.match(TT.QUESTION):
                return TypeNullable(node, line=tok.line, col=tok.col)
            return node

        # fn type
        if self.check(TT.FN):
            self.advance()
            self.expect(TT.LPAREN)
            params = []
            if not self.check(TT.RPAREN):
                params.append(self.parse_type())
                while self.match(TT.COMMA):
                    params.append(self.parse_type())
            self.expect(TT.RPAREN)
            ret = None
            if self.match(TT.ARROW):
                ret = self.parse_type()
            return TypeFn(params, ret, line=tok.line, col=tok.col)

        # Named type: int, str, Stack[T]
        name = self.expect(TT.IDENT).value
        node: Node
        if self.match(TT.LBRACKET):
            params = [self.parse_type()]
            while self.match(TT.COMMA):
                params.append(self.parse_type())
            self.expect(TT.RBRACKET)
            node = TypeGeneric(name, params, line=tok.line, col=tok.col)
        else:
            node = TypeName(name, line=tok.line, col=tok.col)

        if self.match(TT.QUESTION):
            return TypeNullable(node, line=tok.line, col=tok.col)
        return node

    # ── Expression Parser (Pratt / Precedence Climbing) ──────────

    def parse_expr(self, min_prec: int = 0) -> Node:
        left = self.parse_unary()

        while True:
            tok = self.peek()
            prec, assoc = self._get_infix_prec(tok.type)
            if prec <= min_prec:
                break

            # Assignment operatörleri
            if tok.type in (
                TT.ASSIGN,
                TT.PLUS_EQ,
                TT.MINUS_EQ,
                TT.STAR_EQ,
                TT.SLASH_EQ,
            ):
                op = self.advance().type.name
                right = self.parse_expr(0)
                left = AssignExpr(left, op, right, line=tok.line, col=tok.col)
                break

            # Özel: inline if (if..then..else)
            if tok.type == TT.IF:
                self.advance()
                cond = self.parse_expr(0)
                self.expect(TT.THEN)
                then_val = self.parse_expr(0)
                self.expect(TT.ELSE)
                else_val = self.parse_expr(0)
                left = IfExpr(cond, then_val, else_val, line=tok.line, col=tok.col)
                continue

            # Özel: ternary operator (cond ? then : else)
            if tok.type == TT.QUESTION:
                self.advance()
                then_val = self.parse_expr(0)
                self.expect(TT.COLON)
                else_val = self.parse_expr(prec + 1)  # right-associative için prec+1
                left = IfExpr(left, then_val, else_val, line=tok.line, col=tok.col)
                continue

            op_tok = self.advance()
            next_min = prec if assoc == "right" else prec + 1

            # Özel: range
            if op_tok.type == TT.DOTDOT:
                right = self.parse_expr(next_min)
                left = RangeExpr(left, right, line=tok.line, col=tok.col)
                continue

            # Özel: pipe  left |> fn  →  PipeExpr
            if op_tok.type == TT.PIPE:
                right = self.parse_expr(next_min)
                left = PipeExpr(left, right, line=tok.line, col=tok.col)
                continue

            right = self.parse_expr(next_min)
            op_str = self._tok_to_op(op_tok.type)
            left = BinaryExpr(left, op_str, right, line=tok.line, col=tok.col)

        return left

    def _get_infix_prec(self, tt: TT) -> tuple[int, str]:
        table = {
            TT.ASSIGN: (1, "right"),
            TT.PLUS_EQ: (1, "right"),
            TT.MINUS_EQ: (1, "right"),
            TT.STAR_EQ: (1, "right"),
            TT.SLASH_EQ: (1, "right"),
            TT.NULL_COAL: (2, "left"),
            TT.PIPE: (2, "left"),  # |>  (low, left-assoc)
            TT.OR: (3, "left"),
            TT.AND: (4, "left"),
            TT.EQ: (5, "left"),
            TT.NEQ: (5, "left"),
            TT.LT: (6, "left"),
            TT.GT: (6, "left"),
            TT.LTE: (6, "left"),
            TT.GTE: (6, "left"),
            TT.LE: (6, "left"),
            TT.GE: (6, "left"),
            TT.DOTDOT: (7, "left"),
            TT.QUESTION: (8, "right"),  # ternary operator
            TT.PLUS: (9, "left"),
            TT.MINUS: (9, "left"),
            TT.STAR: (10, "left"),
            TT.SLASH: (10, "left"),
            TT.DSLASH: (10, "left"),  # //
            TT.PERCENT: (10, "left"),
            TT.STARSTAR: (11, "right"),
        }
        return table.get(tt, (0, "left"))

    def _tok_to_op(self, tt: TT) -> str:
        return {
            TT.PLUS: "+",
            TT.MINUS: "-",
            TT.STAR: "*",
            TT.SLASH: "/",
            TT.DSLASH: "//",
            TT.PERCENT: "%",
            TT.STARSTAR: "**",
            TT.EQ: "==",
            TT.NEQ: "!=",
            TT.LT: "<",
            TT.GT: ">",
            TT.LTE: "<=",
            TT.GTE: ">=",
            TT.LE: "<=",
            TT.GE: ">=",
            TT.AND: "and",
            TT.OR: "or",
            TT.NULL_COAL: "??",
        }.get(tt, tt.name)

    def parse_unary(self) -> Node:
        tok = self.peek()
        if self.match(TT.MINUS):
            operand = self.parse_unary()
            return UnaryExpr("-", operand, line=tok.line, col=tok.col)
        if self.match(TT.NOT) or self.match(TT.BANG):
            operand = self.parse_unary()
            return UnaryExpr("not", operand, line=tok.line, col=tok.col)
        if self.match(TT.AWAIT):
            expr = self.parse_unary()
            return AwaitExpr(expr, line=tok.line, col=tok.col)
        if self.match(TT.SPAWN):
            expr = self.parse_unary()
            return SpawnExpr(expr, line=tok.line, col=tok.col)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            tok = self.peek()
            if self.match(TT.LPAREN):
                args = []
                if not self.check(TT.RPAREN):
                    args.append(self.parse_expr(0))
                    while self.match(TT.COMMA):
                        args.append(self.parse_expr(0))
                self.expect(TT.RPAREN)
                node = CallExpr(node, args, line=tok.line, col=tok.col)
            elif self.match(TT.LBRACKET):
                index = self.parse_expr(0)
                self.expect(TT.RBRACKET)
                node = IndexExpr(node, index, line=tok.line, col=tok.col)
            elif self.match(TT.DOT):
                member = self.expect(TT.IDENT).value
                node = MemberExpr(node, member, line=tok.line, col=tok.col)
            else:
                break
        return node

    def _parse_fstring_parts(self, raw: str, line: int, col: int) -> list:
        """f"hello {name + 1}!" → ['hello ', <Expr>, '!']"""
        from lexer import Lexer

        parts = []
        i = 0
        buf = ""
        while i < len(raw):
            if raw[i] == "{":
                if buf:
                    parts.append(buf)
                    buf = ""
                i += 1
                depth = 1
                expr_src = ""
                while i < len(raw) and depth > 0:
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                    if depth > 0:
                        expr_src += raw[i]
                    i += 1
                # parse the inner expression
                tokens = Lexer(expr_src).tokenize()
                sub_parser = Parser(tokens)
                parts.append(sub_parser.parse_expr())
            else:
                buf += raw[i]
                i += 1
        if buf:
            parts.append(buf)
        return parts

    def parse_primary(self) -> Node:
        tok = self.peek()

        # Literals
        if self.check(TT.INT):
            return IntLit(self.advance().value, line=tok.line, col=tok.col)
        if self.check(TT.FLOAT):
            return FloatLit(self.advance().value, line=tok.line, col=tok.col)
        if self.check(TT.STRING):
            return StrLit(self.advance().value, line=tok.line, col=tok.col)
        if self.check(TT.FSTRING):
            raw = self.advance().value
            parts = self._parse_fstring_parts(raw, tok.line, tok.col)
            return FStringExpr(parts, line=tok.line, col=tok.col)
        if self.check(TT.BOOL):
            return BoolLit(self.advance().value, line=tok.line, col=tok.col)
        if self.check(TT.NULL):
            self.advance()
            return NullLit(line=tok.line, col=tok.col)

        # Identifier
        if self.check(TT.IDENT):
            name = self.advance().value
            return Ident(name, line=tok.line, col=tok.col)

        if self.check(TT.SELF):
            self.advance()
            return Ident("self", line=tok.line, col=tok.col)
        if self.check(TT.SUPER):
            self.advance()
            return Ident("super", line=tok.line, col=tok.col)
        # Builtin callables: alloc, free, sizeof
        if self.check(TT.ALLOC, TT.FREE, TT.SIZEOF):
            kw = self.advance()
            kw_name = kw.type.name.lower()
            return Ident(kw_name, line=tok.line, col=tok.col)

        # Grouped / Tuple: (expr) veya (a, b, c)
        if self.match(TT.LPAREN):
            first = self.parse_expr(0)
            if self.match(TT.COMMA):
                elems = [first]
                elems.append(self.parse_expr(0))
                while self.match(TT.COMMA):
                    elems.append(self.parse_expr(0))
                self.expect(TT.RPAREN)
                return TupleLit(elems, line=tok.line, col=tok.col)
            self.expect(TT.RPAREN)
            return first

        # Liste: [1, 2, 3]
        if self.match(TT.LBRACKET):
            elems = []
            if not self.check(TT.RBRACKET):
                elems.append(self.parse_expr(0))
                while self.match(TT.COMMA):
                    if self.check(TT.RBRACKET):
                        break
                    elems.append(self.parse_expr(0))
            self.expect(TT.RBRACKET)
            return ListLit(elems, line=tok.line, col=tok.col)

        # Dict/Set: {a: b} veya {a, b}
        if self.match(TT.LBRACE):
            if self.check(TT.RBRACE):
                self.advance()
                return DictLit([], line=tok.line, col=tok.col)
            first_expr = self.parse_expr(0)
            if self.match(TT.COLON):
                # Dict
                val = self.parse_expr(0)
                pairs = [(first_expr, val)]
                while self.match(TT.COMMA):
                    if self.check(TT.RBRACE):
                        break
                    k = self.parse_expr(0)
                    self.expect(TT.COLON)
                    v = self.parse_expr(0)
                    pairs.append((k, v))
                self.expect(TT.RBRACE)
                return DictLit(pairs, line=tok.line, col=tok.col)
            else:
                # Set
                elems = [first_expr]
                while self.match(TT.COMMA):
                    if self.check(TT.RBRACE):
                        break
                    elems.append(self.parse_expr(0))
                self.expect(TT.RBRACE)
                return SetLit(elems, line=tok.line, col=tok.col)

        # Lambda: fn(params) -> ReturnType: expr  veya  fn(params): block
        if self.check(TT.FN):
            self.advance()
            self.expect(TT.LPAREN)
            params = self._parse_params()
            self.expect(TT.RPAREN)
            if self.match(TT.ARROW):
                # -> ReturnType: body_expr  veya  -> ReturnType\n  block
                if not self.check(TT.INDENT) and not self.check(TT.COLON):
                    _ret_type = (
                        self.parse_type()
                    )  # return type ayrıştır, şimdilik kullanılmıyor
                if self.match(TT.COLON):
                    if self.check(TT.INDENT):
                        body = self.parse_block()
                    else:
                        body = self.parse_expr(0)
                elif self.check(TT.INDENT):
                    body = self.parse_block()
                else:
                    body = self.parse_expr(0)
            elif self.check(TT.COLON):
                body = self.parse_block()
            else:
                # Sadece expression - arrow olmadan
                body = self.parse_expr(0)
            return LambdaExpr(params, body, line=tok.line, col=tok.col)

        # Match expression (inline)
        if self.check(TT.MATCH):
            tok2 = self.advance()
            subject = self.parse_expr(0)
            self.expect(TT.COLON)
            self.expect(TT.INDENT)
            arms = []
            while not self.check(TT.DEDENT) and not self.check(TT.EOF):
                self.skip_newlines()
                if self.check(TT.DEDENT):
                    break
                arms.append(self._parse_match_arm())
            self.match(TT.DEDENT)
            return MatchExpr(subject, arms, line=tok2.line, col=tok2.col)

        raise self.error(f"Unexpected token in expression")


# ─── Güzel yazdırıcı ──────────────────────────────────────────────────────────


def pretty_print(node: Node, indent: int = 0) -> str:
    pad = "  " * indent
    name = type(node).__name__

    if isinstance(node, Program):
        lines = [f"{pad}Program"]
        for s in node.stmts:
            lines.append(pretty_print(s, indent + 1))
        return "\n".join(lines)

    if isinstance(node, VarDecl):
        kw = "let" if node.mutable else "const"
        mm = f"@{node.mem_mode} " if node.mem_mode else ""
        ta = f": {pretty_print(node.type_ann, 0)}" if node.type_ann else ""
        val = f" = {pretty_print(node.value, 0)}" if node.value else ""
        return f"{pad}{mm}{kw} {node.name}{ta}{val}"

    if isinstance(node, FnDecl):
        async_ = "async " if node.is_async else ""
        tp = f"[{', '.join(node.type_params)}]" if node.type_params else ""
        params = ", ".join(
            f"{p.name}" + (f": {pretty_print(p.type_ann, 0)}" if p.type_ann else "")
            for p in node.params
        )
        ret = f" -> {pretty_print(node.return_type, 0)}" if node.return_type else ""
        lines = [f"{pad}{async_}fn {node.name}{tp}({params}){ret}:"]
        if node.body:
            for s in node.body.stmts:
                lines.append(pretty_print(s, indent + 1))
        return "\n".join(lines)

    if isinstance(node, IfStmt):
        lines = [f"{pad}if {pretty_print(node.condition, 0)}:"]
        for s in node.then_block.stmts:
            lines.append(pretty_print(s, indent + 1))
        for ec, eb in node.elif_arms:
            lines.append(f"{pad}elif {pretty_print(ec, 0)}:")
            for s in eb.stmts:
                lines.append(pretty_print(s, indent + 1))
        if node.else_block:
            lines.append(f"{pad}else:")
            for s in node.else_block.stmts:
                lines.append(pretty_print(s, indent + 1))
        return "\n".join(lines)

    if isinstance(node, ForStmt):
        lines = [
            f"{pad}for {pretty_print(node.target, 0)} in {pretty_print(node.iterable, 0)}:"
        ]
        for s in node.body.stmts:
            lines.append(pretty_print(s, indent + 1))
        return "\n".join(lines)

    if isinstance(node, ReturnStmt):
        val = f" {pretty_print(node.value, 0)}" if node.value else ""
        return f"{pad}return{val}"

    if isinstance(node, ExprStmt):
        return f"{pad}{pretty_print(node.expr, 0)}"

    if isinstance(node, BinaryExpr):
        return f"({pretty_print(node.left, 0)} {node.op} {pretty_print(node.right, 0)})"

    if isinstance(node, UnaryExpr):
        return f"({node.op} {pretty_print(node.operand, 0)})"

    if isinstance(node, CallExpr):
        args = ", ".join(pretty_print(a, 0) for a in node.args)
        return f"{pretty_print(node.callee, 0)}({args})"

    if isinstance(node, MemberExpr):
        return f"{pretty_print(node.obj, 0)}.{node.member}"

    if isinstance(node, IndexExpr):
        return f"{pretty_print(node.obj, 0)}[{pretty_print(node.index, 0)}]"

    if isinstance(node, AssignExpr):
        return f"{pretty_print(node.target, 0)} {node.op} {pretty_print(node.value, 0)}"

    if isinstance(node, RangeExpr):
        return f"({pretty_print(node.start, 0)}..{pretty_print(node.end, 0)})"

    if isinstance(node, MatchStmt):
        lines = [f"{pad}match {pretty_print(node.subject, 0)}:"]
        for arm in node.arms:
            body = pretty_print(arm.body, 0)
            lines.append(f"{pad}  {pretty_print(arm.pattern, 0)} -> {body}")
        return "\n".join(lines)

    if isinstance(node, ClassDecl):
        tp = f"[{', '.join(node.type_params)}]" if node.type_params else ""
        base = f" extends {node.base}" if node.base else ""
        impls = f" implements {', '.join(node.interfaces)}" if node.interfaces else ""
        lines = [f"{pad}class {node.name}{tp}{base}{impls}:"]
        for f_ in node.fields:
            lines.append(f"{pad}  {f_.name}: {pretty_print(f_.type_ann, 0)}")
        for m in node.methods:
            lines.append(pretty_print(m, indent + 1))
        return "\n".join(lines)

    # Literals & Ident
    if isinstance(node, Ident):
        return node.name
    if isinstance(node, IntLit):
        return str(node.value)
    if isinstance(node, FloatLit):
        return str(node.value)
    if isinstance(node, StrLit):
        return f'"{node.value}"'
    if isinstance(node, BoolLit):
        return "true" if node.value else "false"
    if isinstance(node, NullLit):
        return "null"
    if isinstance(node, ListLit):
        return "[" + ", ".join(pretty_print(e, 0) for e in node.elements) + "]"
    if isinstance(node, TupleLit):
        return "(" + ", ".join(pretty_print(e, 0) for e in node.elements) + ")"
    if isinstance(node, TypeName):
        return node.name
    if isinstance(node, TypeNullable):
        return f"{pretty_print(node.inner, 0)}?"
    if isinstance(node, TypeGeneric):
        p = ", ".join(pretty_print(x, 0) for x in node.params)
        return f"{node.base}[{p}]"
    if isinstance(node, TypeRef):
        mut = "mut " if node.mutable else ""
        return f"&{mut}{pretty_print(node.inner, 0)}"

    return f"{pad}{name}(...)"
