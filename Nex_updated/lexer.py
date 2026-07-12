"""
Nex Language — Lexer
Source code'i token'lara ayırır.
"""

from enum import Enum, auto
from dataclasses import dataclass
from typing import List


class TT(Enum):
    # Literals
    INT = auto()
    FLOAT = auto()
    STR = auto()
    STRING = auto()
    BOOL = auto()
    NULL = auto()
    IDENT = auto()
    
    # Keywords
    LET = auto()
    CONST = auto()
    FN = auto()
    RETURN = auto()
    IF = auto()
    ELSE = auto()
    ELIF = auto()
    FOR = auto()
    IN = auto()
    WHILE = auto()
    MATCH = auto()
    IMPORT = auto()
    MODULE = auto()
    CLASS = auto()
    INTERFACE = auto()
    EXTENDS = auto()
    IMPLEMENTS = auto()
    ASYNC = auto()
    AWAIT = auto()
    SPAWN = auto()
    EXPORT = auto()
    OWN = auto()
    RAW = auto()
    STACK = auto()
    TRY = auto()
    CATCH = auto()
    FINALLY = auto()
    BREAK = auto()
    CONTINUE = auto()
    AS = auto()
    AMP = auto()
    BANG = auto()
    SELF = auto()
    SUPER = auto()
    THEN = auto()
    UNDERSCORE = auto()
    ALLOC = auto()
    FREE = auto()
    ASS = auto()
    QUEST = auto()
    SIZEOF = auto()
    
    # Operators
    ASSIGN = auto()
    PLUS_EQ = auto()
    MINUS_EQ = auto()
    STAR_EQ = auto()
    SLASH_EQ = auto()
    PLUS = auto()
    MINUS = auto()
    MUL = auto()
    STAR = auto()
    STARSTAR = auto()
    DIV = auto()
    SLASH = auto()
    MOD = auto()
    PERCENT = auto()
    EQ = auto()
    NE = auto()
    NEQ = auto()
    LTE = auto()
    GTE = auto()
    LT = auto()
    GT = auto()
    LE = auto()
    GE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    ARROW = auto()
    NULL_COAL = auto()
    DSLASH = auto()   # //  integer division
    PIPE = auto()     # |>  pipe operator
    FSTRING = auto()  # f"..." interpolated string
    
    # Delimiters
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    DOTDOT = auto()
    DOT = auto()
    COLON = auto()
    QUESTION = auto()
    
    # Special
    NEWLINE = auto()
    INDENT = auto()
    DEDENT = auto()
    EOF = auto()


@dataclass
class Token:
    type: TT
    value: str = ""
    line: int = 1
    col: int = 1
    
    def __repr__(self):
        return f"Token({self.type.name}, {self.value!r})"


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []
        self.indent_stack = [0]
        
        self.keywords = {
            'let': TT.LET,
            'const': TT.CONST,
            'fn': TT.FN,
            'return': TT.RETURN,
            'if': TT.IF,
            'else': TT.ELSE,
            'elif': TT.ELIF,
            'for': TT.FOR,
            'in': TT.IN,
            'while': TT.WHILE,
            'match': TT.MATCH,
            'import': TT.IMPORT,
            'module': TT.MODULE,
            'class': TT.CLASS,
            'interface': TT.INTERFACE,
            'extends': TT.EXTENDS,
            'implements': TT.IMPLEMENTS,
            'async': TT.ASYNC,
            'await': TT.AWAIT,
            'spawn': TT.SPAWN,
            'export': TT.EXPORT,
            'own': TT.OWN,
            'raw': TT.RAW,
            'stack': TT.STACK,
            'try': TT.TRY,
            'catch': TT.CATCH,
            'finally': TT.FINALLY,
            'break': TT.BREAK,
            'continue': TT.CONTINUE,
            'as': TT.AS,
            'self': TT.SELF,
            'super': TT.SUPER,
            'then': TT.THEN,
            '_': TT.UNDERSCORE,
            'alloc': TT.ALLOC,
            'free': TT.FREE,
            'ass': TT.ASS,
            'quest': TT.QUEST,
            'sizeof': TT.SIZEOF,
            'and': TT.AND,
            'or': TT.OR,
            'not': TT.NOT,
            'true': TT.BOOL,
            'false': TT.BOOL,
            'null': TT.NULL,
        }
    
    def peek(self, offset: int = 0) -> str:
        pos = self.pos + offset
        if pos < len(self.source):
            return self.source[pos]
        return '\0'
    
    def advance(self) -> str:
        ch = self.peek()
        self.pos += 1
        if ch == '\n':
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch
    
    def skip_whitespace(self):
        while self.peek() in ' \t':
            self.advance()
    
    def skip_comment(self):
        if self.peek() == '#':
            while self.peek() not in '\n\0':
                self.advance()
    
    def read_number(self) -> Token:
        start_line, start_col = self.line, self.col
        value = ""
        has_dot = False
        
        while self.peek() and (self.peek().isdigit() or self.peek() == '.'):
            if self.peek() == '.':
                if has_dot or self.peek(1) == '.':
                    break
                has_dot = True
            value += self.advance()
        
        token_type = TT.FLOAT if has_dot else TT.INT
        return Token(token_type, value, start_line, start_col)
    
    def read_string(self) -> Token:
        start_line, start_col = self.line, self.col
        quote = self.advance()  # " or '
        value = ""
        
        while self.peek() != quote and self.peek() != '\0':
            if self.peek() == '\\':
                self.advance()
                esc = self.advance()
                if esc == 'n': value += '\n'
                elif esc == 't': value += '\t'
                elif esc == 'r': value += '\r'
                elif esc == '\\': value += '\\'
                elif esc == '"': value += '"'
                elif esc == "'": value += "'"
                else: value += esc
            else:
                value += self.advance()
        
        self.advance()  # closing quote
        return Token(TT.STRING, value, start_line, start_col)
    
    def read_fstring(self) -> Token:
        """Tokenize f"...{expr}..." — stores raw template string as FSTRING token."""
        start_line, start_col = self.line, self.col
        quote = self.advance()  # opening quote
        value = ""
        while self.peek() != quote and self.peek() != '\0':
            if self.peek() == '\\':
                self.advance()
                esc = self.advance()
                if esc == 'n': value += '\n'
                elif esc == 't': value += '\t'
                elif esc == '\\': value += '\\'
                elif esc == '"': value += '"'
                elif esc == "'": value += "'"
                else: value += esc
            else:
                value += self.advance()
        self.advance()  # closing quote
        return Token(TT.FSTRING, value, start_line, start_col)
    
    def read_ident(self) -> Token:
        start_line, start_col = self.line, self.col
        value = ""
        
        while (self.peek() and (self.peek().isalnum() or self.peek() == '_')):
            value += self.advance()
        
        token_type = self.keywords.get(value, TT.IDENT)
        return Token(token_type, value, start_line, start_col)
    
    def handle_indent(self):
        indent_level = 0
        while self.peek() in ' \t':
            if self.peek() == ' ':
                indent_level += 1
            else:  # tab = 4 spaces
                indent_level += 4
            self.advance()
        
        if self.peek() == '\n':
            return  # empty line
        
        if indent_level > self.indent_stack[-1]:
            self.indent_stack.append(indent_level)
            self.tokens.append(Token(TT.INDENT, line=self.line, col=self.col))
        else:
            while indent_level < self.indent_stack[-1]:
                self.indent_stack.pop()
                self.tokens.append(Token(TT.DEDENT, line=self.line, col=self.col))
    
    def tokenize(self) -> List[Token]:
        while self.pos < len(self.source):
            self.skip_whitespace()
            self.skip_comment()
            
            ch = self.peek()
            if ch == '\0':
                break
            
            start_line, start_col = self.line, self.col
            
            # Newline and indentation
            if ch == '\n':
                self.advance()
                self.tokens.append(Token(TT.NEWLINE, line=start_line, col=start_col))
                self.handle_indent()
                continue
            
            # Numbers
            if ch.isdigit():
                self.tokens.append(self.read_number())
                continue
            
            # Strings
            if ch == 'f' and self.peek(1) in ('"', "'"):
                self.advance()
                self.tokens.append(self.read_fstring())
                continue
            if ch in '"\'':
                self.tokens.append(self.read_string())
                continue
            
            # Identifiers and keywords
            if ch.isalpha() or ch == '_':
                self.tokens.append(self.read_ident())
                continue
            
            # Two-character operators
            if ch == '=' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.EQ, '==', start_line, start_col))
                continue
            elif ch == '!' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.NEQ, '!=', start_line, start_col))
                continue
            elif ch == '<' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.LE, '<=', start_line, start_col))
                continue
            elif ch == '>' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.GE, '>=', start_line, start_col))
                continue
            elif ch == '-' and self.peek(1) == '>':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.ARROW, '->', start_line, start_col))
                continue
            elif ch == '?' and self.peek(1) == '?':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.NULL_COAL, '??', start_line, start_col))
                continue
            elif ch == '+' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.PLUS_EQ, '+=', start_line, start_col))
                continue
            elif ch == '-' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.MINUS_EQ, '-=', start_line, start_col))
                continue
            elif ch == '*' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.STAR_EQ, '*=', start_line, start_col))
                continue
            elif ch == '/' and self.peek(1) == '=':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.SLASH_EQ, '/=', start_line, start_col))
                continue
            elif ch == '/' and self.peek(1) == '/':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.DSLASH, '//', start_line, start_col))
                continue
            elif ch == '|' and self.peek(1) == '>':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.PIPE, '|>', start_line, start_col))
                continue
            elif ch == '*' and self.peek(1) == '*':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.STARSTAR, '**', start_line, start_col))
                continue
            elif ch == '.' and self.peek(1) == '.':
                self.advance(); self.advance()
                self.tokens.append(Token(TT.DOTDOT, '..', start_line, start_col))
                continue
            
            # Single-character tokens
            single_char_map = {
                '+': TT.PLUS, '-': TT.MINUS, '*': TT.STAR, '/': TT.SLASH,
                '%': TT.PERCENT, '<': TT.LT, '>': TT.GT, '(': TT.LPAREN,
                ')': TT.RPAREN, '[': TT.LBRACKET, ']': TT.RBRACKET,
                '{': TT.LBRACE, '}': TT.RBRACE, ',': TT.COMMA,
                '.': TT.DOT, ':': TT.COLON, '?': TT.QUESTION,
                '|': TT.OR,
                '&': TT.AMP,
                '!': TT.BANG,
                '=': TT.ASSIGN,
            }
            
            if ch in single_char_map:
                self.advance()
                self.tokens.append(Token(single_char_map[ch], ch, start_line, start_col))
                continue
            
            # Unknown character
            raise SyntaxError(f"Unexpected character '{ch}' at {start_line}:{start_col}")
        
        # Close remaining indent blocks
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self.tokens.append(Token(TT.DEDENT, line=self.line, col=self.col))
        
        self.tokens.append(Token(TT.EOF, line=self.line, col=self.col))
        return self.tokens
