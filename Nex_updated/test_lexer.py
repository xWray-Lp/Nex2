#!/usr/bin/env python3
"""
Test the Nex lexer with basic examples.
"""

from lexer import Lexer, TT

def test_basic_tokens():
    code = """let x = 10
const y = 3.14
let name = "Nex"
let flag = true
let nothing = null"""
    
    lexer = Lexer(code)
    tokens = lexer.tokenize()
    
    print("=== Basic Tokens Test ===")
    for token in tokens:
        if token.type not in (TT.NEWLINE, TT.INDENT, TT.DEDENT, TT.EOF):
            print(f"{token}")

def test_indentation():
    code = """if true:
    let x = 1
    if x > 0:
        print(x)
    else:
        print("zero")
print("done")"""
    
    lexer = Lexer(code)
    tokens = lexer.tokenize()
    
    print("\n=== Indentation Test ===")
    for token in tokens:
        if token.type in (TT.INDENT, TT.DEDENT):
            print(f"{token}")

def test_operators():
    code = "a + b * c == d / e and f != g or not h"
    
    lexer = Lexer(code)
    tokens = lexer.tokenize()
    
    print("\n=== Operators Test ===")
    for token in tokens:
        print(f"{token}")

def test_function():
    code = """fn add(x: int, y: int) -> int:
    return x + y

let result = add(5, 3)"""
    
    lexer = Lexer(code)
    tokens = lexer.tokenize()
    
    print("\n=== Function Test ===")
    for token in tokens:
        if token.type not in (TT.NEWLINE, TT.EOF):
            print(f"{token}")

if __name__ == "__main__":
    test_basic_tokens()
    test_indentation()
    test_operators()
    test_function()
