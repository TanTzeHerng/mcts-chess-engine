#!/usr/bin/env python3
"""Benchmark harness for running matches against Maia 1100."""

import subprocess
import sys
import os
import re
import json
import time
from pathlib import Path

# PGN template for match setup
PGN_TEMPLATE = """[Event "MCTS vs Maia Benchmark"]
[Site "localhost"]
[Date "{date}"]
[White "{white}"]
[Black "{black}"]
[Result "{result}"]

{moves}
"""

def run_game(engine_path: str, maia_path: str, white_engine: str, time_per_move_ms: int = 1000):
    """Run a single game between two engines."""
    print(f"[Game] {white_engine} (W) vs Maia (B)")
    
    # Start both engines
    try:
        mcts_proc = subprocess.Popen(
            [sys.executable, engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        maia_proc = subprocess.Popen(
            [maia_path, '--eval-file', 'weights.bin'] if os.path.exists('weights.bin') else [maia_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )
    except FileNotFoundError as e:
        print(f"[Error] Could not find engine: {e}")
        return None
    except Exception as e:
        print(f"[Error] Engine startup failed: {e}")
        return None
    
    # Communicate with engines via UCI
    moves = []
    position = "startpos"
    
    try:
        # Initialize both engines
        mcts_proc.stdin.write("uci\n")
        maia_proc.stdin.write("uci\n")
        mcts_proc.stdin.flush()
        maia_proc.stdin.flush()
        
        # Wait for uciok
        time.sleep(0.5)
        
        # Game loop (max 300 plies)
        for ply in range(300):
            current_engine = mcts_proc if (ply % 2 == 0 and white_engine == "MCTS") or (ply % 2 == 1 and white_engine == "Maia") else maia_proc
            other_engine = maia_proc if current_engine == mcts_proc else mcts_proc
            
            # Set position
            cmd = f"position {position} moves {' '.join(moves)}\n"
            current_engine.stdin.write(cmd)
            current_engine.stdin.flush()
            
            # Get move
            go_cmd = f"go movetime {time_per_move_ms}\n"
            current_engine.stdin.write(go_cmd)
            current_engine.stdin.flush()
            
            # Read response
            move = None
            start_read = time.time()
            while time.time() - start_read < 60:
                line = current_engine.stdout.readline()
                if "bestmove" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        move = parts[1]
                    break
            
            if move is None or move == "(none)":
                print(f"[Game End] {current_engine.args[1] if hasattr(current_engine, 'args') else 'Engine'} has no legal moves")
                result = "0-1" if current_engine == mcts_proc else "1-0"
                return result
            
            moves.append(move)
            print(f"  Ply {ply + 1}: {move}")
    
    except Exception as e:
        print(f"[Error] Game loop error: {e}")
        result = None
    finally:
        mcts_proc.terminate()
        maia_proc.terminate()
    
    return result or "1/2-1/2"


def run_match(engine_path: str, maia_path: str, num_games: int = 20, time_per_move_ms: int = 1000):
    """Run a match with multiple games."""
    print(f"\n=== MCTS vs Maia 1100 Benchmark ===")
    print(f"Engine: {engine_path}")
    print(f"Maia: {maia_path}")
    print(f"Games: {num_games}")
    print(f"Time per move: {time_per_move_ms}ms\n")
    
    results = {"mcts_wins": 0, "draws": 0, "maia_wins": 0, "games": []}
    
    for game_num in range(num_games):
        # Alternate colors each game
        is_mcts_white = (game_num % 2 == 0)
        white_engine = "MCTS" if is_mcts_white else "Maia"
        
        result = run_game(engine_path, maia_path, white_engine, time_per_move_ms)
        
        if result == "1-0":
            if is_mcts_white:
                results["mcts_wins"] += 1
            else:
                results["maia_wins"] += 1
        elif result == "0-1":
            if is_mcts_white:
                results["maia_wins"] += 1
            else:
                results["mcts_wins"] += 1
        else:
            results["draws"] += 1
        
        results["games"].append({"game": game_num + 1, "white": white_engine, "result": result})
        
        mcts_score = results["mcts_wins"] + 0.5 * results["draws"]
        total_games = game_num + 1
        print(f"\n[Match Progress] MCTS: {mcts_score}/{total_games}")
    
    print(f"\n=== Final Results ===")
    print(f"MCTS Wins: {results['mcts_wins']}")
    print(f"Draws: {results['draws']}")
    print(f"Maia Wins: {results['maia_wins']}")
    mcts_score = results["mcts_wins"] + 0.5 * results["draws"]
    print(f"MCTS Score: {mcts_score}/{num_games} ({100*mcts_score/num_games:.1f}%)")
    
    return results


if __name__ == "__main__":
    engine_path = "engine.py"
    maia_path = os.environ.get("MAIA_PATH", "lc0")  # Default to lc0 (Leela Chess Zero)
    num_games = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    time_per_move = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    
    results = run_match(engine_path, maia_path, num_games, time_per_move)
    
    # Save results
    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to results.json")
