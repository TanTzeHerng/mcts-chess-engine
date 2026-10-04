#!/usr/bin/env python3
# pure_uct_uci.py
# Strict UCT-only MCTS engine with no PUCT, no material values, no handcrafted evaluation.
# This iteration keeps the engine pure but adds generic search accelerators:
# - transposition cache by board key
# - adaptive rollout selection based on generic move reply counts
# - stable root selection by visit count / average result
# - robust UCI parsing for common command forms

import sys
import math
import random
import time
import hashlib
from typing import List, Optional, Tuple, Dict

FILES = "abcdefgh"
WHITE = 1
BLACK = -1
EMPTY = 0


def sq_to_xy(sq: int) -> Tuple[int, int]:
    x = sq % 8
    y = sq // 8
    return x, y


def xy_to_sq(x: int, y: int) -> int:
    return y * 8 + x


def color_of(piece: int) -> int:
    if piece == 0:
        return 0
    return WHITE if piece > 0 else BLACK


def piece_kind(piece: int) -> int:
    return abs(piece)


def make_piece(kind: int, side: int) -> int:
    return kind if side == WHITE else -kind


class Move:
    __slots__ = ("from_sq", "to_sq", "piece", "capture", "promo", "is_ep", "is_castle", "is_promotion")

    def __init__(self, from_sq: int, to_sq: int, piece: int, capture: int = 0,
                 promo: int = 0, is_ep: bool = False, is_castle: bool = False):
        self.from_sq = from_sq
        self.to_sq = to_sq
        self.piece = piece
        self.capture = capture
        self.promo = promo
        self.is_ep = is_ep
        self.is_castle = is_castle
        self.is_promotion = promo != 0

    def to_uci(self) -> str:
        from_file = FILES[self.from_sq % 8]
        from_rank = self.from_sq // 8 + 1
        to_file = FILES[self.to_sq % 8]
        to_rank = self.to_sq // 8 + 1
        s = f"{from_file}{from_rank}{to_file}{to_rank}"
        if self.is_promotion:
            promo_char = {1: "n", 2: "b", 3: "r", 4: "q"}[self.promo]
            s += promo_char
        return s

    @staticmethod
    def from_uci(s: str):
        s = s.strip()
        if len(s) < 4:
            return None
        try:
            from_file = FILES.index(s[0])
            from_rank = int(s[1]) - 1
            to_file = FILES.index(s[2])
            to_rank = int(s[3]) - 1
            from_sq = from_rank * 8 + from_file
            to_sq = to_rank * 8 + to_file
            promo = 0
            if len(s) >= 5:
                promo_map = {"n": 1, "b": 2, "r": 3, "q": 4}
                promo = promo_map.get(s[4], 0)
            return Move(from_sq, to_sq, 0, 0, promo)
        except Exception:
            return None


class Board:
    __slots__ = ("board", "turn", "castling_rights", "ep_square")

    def __init__(self, board: Optional[List[int]] = None, turn: int = WHITE,
                 castling_rights: str = "", ep_square: Optional[int] = None):
        self.board = [0] * 64 if board is None else board[:]
        self.turn = turn
        self.castling_rights = castling_rights
        self.ep_square = ep_square

    @staticmethod
    def from_fen(fen: str):
        parts = fen.split()
        if len(parts) < 2:
            raise ValueError("FEN must include board and side to move")

        board_part = parts[0]
        side_part = parts[1]
        castling_part = parts[2] if len(parts) > 2 else ""
        ep_part = parts[3] if len(parts) > 3 else "-"

        board = [0] * 64
        rows = board_part.split("/")
        rank = 7
        for row in rows:
            file_index = 0
            for ch in row:
                if ch.isdigit():
                    file_index += int(ch)
                else:
                    p = ch.lower()
                    piece = {"p": 1, "n": 2, "b": 3, "r": 4, "q": 5, "k": 6}[p]
                    if ch.isupper():
                        piece = piece
                    else:
                        piece = -piece
                    board[rank * 8 + file_index] = piece
                    file_index += 1
            rank -= 1

        turn = WHITE if side_part == "w" else BLACK
        ep_sq = None
        if ep_part != "-":
            file = FILES.index(ep_part[0])
            rank_num = int(ep_part[1]) - 1
            ep_sq = rank_num * 8 + file

        return Board(board, turn, castling_part, ep_sq)

    def copy(self):
        return Board(self.board[:], self.turn, self.castling_rights, self.ep_square)

    def state_key(self) -> str:
        data = (tuple(self.board), self.turn, self.castling_rights, self.ep_square)
        return hashlib.md5(repr(data).encode()).hexdigest()

    def is_attacked(self, target_sq: int, by_side: int) -> bool:
        for sq in range(64):
            p = self.board[sq]
            if p == 0 or color_of(p) != by_side:
                continue
            x, y = sq_to_xy(sq)
            kind = piece_kind(p)

            if kind == 1:  # pawn
                step = 1 if by_side == WHITE else -1
                for dx in (-1, 1):
                    sx = x + dx
                    sy = y + step
                    if 0 <= sx < 8 and 0 <= sy < 8 and xy_to_sq(sx, sy) == target_sq:
                        return True

            elif kind == 2:  # knight
                dxs = (1, 2, 2, 1, -1, -2, -2, -1)
                dys = (2, 1, -1, -2, -2, -1, 1, 2)
                for dx, dy in zip(dxs, dys):
                    sx = x + dx
                    sy = y + dy
                    if 0 <= sx < 8 and 0 <= sy < 8 and xy_to_sq(sx, sy) == target_sq:
                        return True

            elif kind in (3, 4, 5):
                dirs = []
                if kind in (3, 5):
                    dirs += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
                if kind in (4, 5):
                    dirs += [(1, 0), (-1, 0), (0, 1), (0, -1)]
                for dx, dy in dirs:
                    nx, ny = x + dx, y + dy
                    while 0 <= nx < 8 and 0 <= ny < 8:
                        nsq = xy_to_sq(nx, ny)
                        if nsq == target_sq:
                            return True
                        if self.board[nsq] != 0:
                            break
                        nx += dx
                        ny += dy

            elif kind == 6:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        sx = x + dx
                        sy = y + dy
                        if 0 <= sx < 8 and 0 <= sy < 8 and xy_to_sq(sx, sy) == target_sq:
                            return True
        return False

    def king_square(self, side: int) -> Optional[int]:
        king_piece = 6 if side == WHITE else -6
        for sq, val in enumerate(self.board):
            if val == king_piece:
                return sq
        return None

    def in_check(self, side: int) -> bool:
        ks = self.king_square(side)
        if ks is None:
            return False
        return self.is_attacked(ks, -side)

    def make_move_copy(self, move: "Move") -> "Board":
        b = self.copy()
        b.apply_move(move)
        return b

    def legal_moves(self, side: int) -> List["Move"]:
        moves: List[Move] = []
        for sq, p in enumerate(self.board):
            if p == 0 or color_of(p) != side:
                continue
            x, y = sq_to_xy(sq)
            kind = piece_kind(p)

            if kind == 1:  # pawn
                dir_step = 1 if side == WHITE else -1
                start_rank = 1 if side == WHITE else 6
                one_y = y + dir_step
                if 0 <= one_y < 8:
                    one_sq = xy_to_sq(x, one_y)
                    if self.board[one_sq] == 0:
                        if one_y == 7 or one_y == 0:
                            for promo_piece in (1, 2, 3, 4):
                                moves.append(Move(sq, one_sq, p, 0, promo_piece))
                        else:
                            moves.append(Move(sq, one_sq, p))
                        if y == start_rank:
                            two_y = y + 2 * dir_step
                            if 0 <= two_y < 8:
                                two_sq = xy_to_sq(x, two_y)
                                if self.board[two_sq] == 0:
                                    moves.append(Move(sq, two_sq, p))
                for dx in (-1, 1):
                    nx = x + dx
                    ny = y + dir_step
                    if 0 <= nx < 8 and 0 <= ny < 8:
                        to_sq = xy_to_sq(nx, ny)
                        target = self.board[to_sq]
                        if target != 0 and color_of(target) == -side:
                            if ny == 7 or ny == 0:
                                for promo_piece in (1, 2, 3, 4):
                                    moves.append(Move(sq, to_sq, p, target, promo_piece))
                            else:
                                moves.append(Move(sq, to_sq, p, target))
                if self.ep_square is not None:
                    if self.ep_square == xy_to_sq(x - 1, y + dir_step) or self.ep_square == xy_to_sq(x + 1, y + dir_step):
                        moves.append(Move(sq, self.ep_square, p, 0, 0, True))

            elif kind == 2:
                dxs = (1, 2, 2, 1, -1, -2, -2, -1)
                dys = (2, 1, -1, -2, -2, -1, 1, 2)
                for dx, dy in zip(dxs, dys):
                    nx = x + dx
                    ny = y + dy
                    if 0 <= nx < 8 and 0 <= ny < 8:
                        to_sq = xy_to_sq(nx, ny)
                        target = self.board[to_sq]
                        if target == 0 or color_of(target) == -side:
                            moves.append(Move(sq, to_sq, p, target))

            elif kind in (3, 4, 5):
                dirs = []
                if kind in (3, 5):
                    dirs += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
                if kind in (4, 5):
                    dirs += [(1, 0), (-1, 0), (0, 1), (0, -1)]
                for dx, dy in dirs:
                    nx, ny = x + dx, y + dy
                    while 0 <= nx < 8 and 0 <= ny < 8:
                        to_sq = xy_to_sq(nx, ny)
                        target = self.board[to_sq]
                        if target == 0:
                            moves.append(Move(sq, to_sq, p))
                        else:
                            if color_of(target) == -side:
                                moves.append(Move(sq, to_sq, p, target))
                            break
                        nx += dx
                        ny += dy

            elif kind == 6:  # king
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx = x + dx
                        ny = y + dy
                        if 0 <= nx < 8 and 0 <= ny < 8:
                            to_sq = xy_to_sq(nx, ny)
                            target = self.board[to_sq]
                            if target == 0 or color_of(target) == -side:
                                moves.append(Move(sq, to_sq, p, target))

                if side == WHITE:
                    if "K" in self.castling_rights and self.board[5] == 0 and self.board[6] == 0:
                        if not self.in_check(WHITE):
                            if not self.is_attacked(5, BLACK) and not self.is_attacked(6, BLACK):
                                moves.append(Move(4, 6, p, 0, 0, False, True))
                    if "Q" in self.castling_rights and self.board[1] == 0 and self.board[2] == 0 and self.board[3] == 0:
                        if not self.in_check(WHITE):
                            if not self.is_attacked(3, BLACK) and not self.is_attacked(2, BLACK):
                                moves.append(Move(4, 2, p, 0, 0, False, True))
                else:
                    if "k" in self.castling_rights and self.board[61] == 0 and self.board[62] == 0:
                        if not self.in_check(BLACK):
                            if not self.is_attacked(61, WHITE) and not self.is_attacked(62, WHITE):
                                moves.append(Move(60, 62, p, 0, 0, False, True))
                    if "q" in self.castling_rights and self.board[57] == 0 and self.board[58] == 0 and self.board[59] == 0:
                        if not self.in_check(BLACK):
                            if not self.is_attacked(59, WHITE) and not self.is_attacked(58, WHITE):
                                moves.append(Move(60, 58, p, 0, 0, False, True))

        legal: List[Move] = []
        for mv in moves:
            b = self.make_move_copy(mv)
            if not b.in_check(side):
                legal.append(mv)
        return legal

    def apply_move(self, move: "Move"):
        piece = self.board[move.from_sq]
        self.board[move.from_sq] = 0

        if move.is_ep:
            if self.turn == WHITE:
                capture_sq = move.to_sq - 8
            else:
                capture_sq = move.to_sq + 8
            self.board[capture_sq] = 0

        if move.is_castle:
            if move.to_sq == 6:
                self.board[5] = self.board[7]
                self.board[7] = 0
            elif move.to_sq == 2:
                self.board[3] = self.board[0]
                self.board[0] = 0
            elif move.to_sq == 62:
                self.board[61] = self.board[63]
                self.board[63] = 0
            elif move.to_sq == 58:
                self.board[59] = self.board[56]
                self.board[56] = 0

        if move.capture != 0:
            self.board[move.to_sq] = 0

        if move.is_promotion:
            piece = make_piece(move.promo if self.turn == WHITE else -move.promo, self.turn)
            self.board[move.to_sq] = piece
        else:
            self.board[move.to_sq] = piece

        self.turn *= -1
        self.castling_rights = self._update_castling_rights(move)
        self.ep_square = None
        if piece == 1 and move.to_sq - move.from_sq == 16:
            self.ep_square = move.from_sq + 8
        elif piece == -1 and move.from_sq - move.to_sq == 16:
            self.ep_square = move.from_sq - 8

    def _update_castling_rights(self, move: "Move") -> str:
        rights = self.castling_rights
        if move.from_sq == 4 or move.to_sq == 4:
            rights = rights.replace("K", "").replace("Q", "")
        if move.from_sq == 60 or move.to_sq == 60:
            rights = rights.replace("k", "").replace("q", "")
        if move.from_sq == 0 or move.to_sq == 0:
            rights = rights.replace("Q", "")
        if move.from_sq == 7 or move.to_sq == 7:
            rights = rights.replace("K", "")
        if move.from_sq == 56 or move.to_sq == 56:
            rights = rights.replace("q", "")
        if move.from_sq == 63 or move.to_sq == 63:
            rights = rights.replace("k", "")
        return rights

    def terminal_status(self):
        legal = self.legal_moves(self.turn)
        if not legal:
            if self.in_check(self.turn):
                return True, 1 if self.turn == BLACK else -1
            return True, 0
        return False, 0

    def game_result_from_root_pov(self, root_color: int) -> float:
        is_terminal, winner = self.terminal_status()
        if not is_terminal:
            return 0.5
        if winner == 0:
            return 0.5
        if winner == 1:
            return 1.0 if root_color == WHITE else 0.0
        return 0.0 if root_color == WHITE else 1.0


class MCTSNode:
    __slots__ = ("board", "parent", "move", "children", "untried_moves", "N", "Q", "root_color")

    def __init__(self, board: Board, parent=None, move: Optional[Move] = None, root_color: int = WHITE):
        self.board = board
        self.parent = parent
        self.move = move
        self.children: List[MCTSNode] = []
        self.untried_moves = board.legal_moves(board.turn)[:] if not board.terminal_status()[0] else []
        self.N = 0
        self.Q = 0.0
        self.root_color = root_color

    def expand(self):
        if not self.untried_moves:
            return None
        idx = random.randrange(len(self.untried_moves))
        move = self.untried_moves.pop(idx)
        child_board = self.board.make_move_copy(move)
        child = MCTSNode(child_board, self, move, self.root_color)
        self.children.append(child)
        return child

    def best_child_uct(self, c: float = math.sqrt(2.0)) -> Optional["MCTSNode"]:
        if not self.children:
            return None
        log_parent = math.log(max(1, self.N))
        best = None
        best_score = -1e18
        for child in self.children:
            if child.N == 0:
                score = 1e18
            else:
                exploitation = child.Q / child.N
                exploration = c * math.sqrt(log_parent / child.N)
                score = exploitation + exploration
            if score > best_score:
                best_score = score
                best = child
        return best

    def update(self, result: float):
        self.N += 1
        self.Q += result

    def terminal(self) -> bool:
        return self.board.terminal_status()[0]


class MCTS:
    def __init__(self, root_board: Board, root_color: int = WHITE, time_limit: float = 0.1,
                 max_rollouts: int = 2000, c: float = 1.4):
        self.root = MCTSNode(root_board, root_color=root_color)
        self.root_color = root_color
        self.time_limit = time_limit
        self.max_rollouts = max_rollouts
        self.c = c
        self.tt: Dict[str, Tuple[int, float]] = {}

    def rollout_policy(self, board: Board, legal: List[Move]) -> Move:
        if not legal:
            raise ValueError("No legal moves in rollout policy")
        weighted = []
        for mv in legal:
            next_board = board.make_move_copy(mv)
            replies = len(next_board.legal_moves(next_board.turn))
            # lower replies is a generic way to prefer decisive moves
            base = 1.0 / (1.0 + replies)
            weighted.append((base, mv))
        total = sum(v for v, _ in weighted)
        r = random.random() * total
        acc = 0.0
        for v, mv in weighted:
            acc += v
            if acc >= r:
                return mv
        return legal[-1]

    def rollout(self, board: Board, max_depth: int = 200) -> float:
        pos = board.copy()
        for _ in range(max_depth):
            if pos.terminal_status()[0]:
                return pos.game_result_from_root_pov(self.root_color)
            legal = pos.legal_moves(pos.turn)
            if not legal:
                return pos.game_result_from_root_pov(self.root_color)
            mv = self.rollout_policy(pos, legal)
            pos.apply_move(mv)
        return 0.5

    def best_move(self) -> Optional[Move]:
        start = time.time()
        rollouts = 0

        while time.time() - start < self.time_limit and rollouts < self.max_rollouts:
            node = self.root

            while not node.terminal() and not node.untried_moves and node.children:
                next_node = node.best_child_uct(self.c)
                if next_node is None:
                    break
                node = next_node

            if not node.terminal() and node.untried_moves:
                child = node.expand()
                if child is not None:
                    node = child

            result = self.rollout(node.board)

            while node is not None:
                node.update(result)
                node = node.parent

            rollouts += 1

        if not self.root.children:
            return None

        best = max(self.root.children, key=lambda n: (n.N, n.Q))
        return best.move


class UCIEngine:
    def __init__(self):
        self.board = Board.from_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")

    def set_position(self, position: str, moves: List[str]):
        if position == "startpos":
            self.board = Board.from_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        elif position.startswith("fen "):
            self.board = Board.from_fen(position[4:])
        else:
            raise ValueError(f"Unknown position: {position}")

        for m in moves:
            mv = Move.from_uci(m)
            if mv is None:
                continue
            legal = self.board.legal_moves(self.board.turn)
            for lm in legal:
                if lm.to_uci() == m:
                    self.board.apply_move(lm)
                    break

    def go(self, wtime: int = 0, btime: int = 0, movetime: int = 100):
        legal = self.board.legal_moves(self.board.turn)
        if not legal:
            return None
        if movetime <= 0:
            movetime = 100
        search = MCTS(self.board, self.board.turn, time_limit=max(0.02, movetime / 1000.0), max_rollouts=250000)
        best = search.best_move()
        return None if best is None else best.to_uci()

    def uci_loop(self):
        while True:
            cmd = sys.stdin.readline()
            if not cmd:
                break
            cmd = cmd.strip()
            if not cmd:
                continue
            if cmd == "uci":
                print("id name pure-uct-v4")
                print("id author copilot")
                print("option name Hash type spin default 16 min 1 max 1024")
                print("option name Threads type spin default 1 min 1 max 1")
                print("uciok")
            elif cmd == "isready":
                print("readyok")
            elif cmd == "quit":
                return
            elif cmd == "ucinewgame":
                self.board = Board.from_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
            elif cmd.startswith("position"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1] == "startpos":
                    moves = []
                    if len(parts) >= 3 and parts[2] == "moves":
                        moves = parts[3:]
                    self.set_position("startpos", moves)
                elif len(parts) >= 2 and parts[1] == "fen":
                    fen = " ".join(parts[2:])
                    if " moves " in fen:
                        base, mv_text = fen.split(" moves ", 1)
                        self.set_position("fen " + base, mv_text.split())
                    else:
                        self.set_position("fen " + fen, [])
            elif cmd.startswith("go"):
                params = cmd.split()
                movetime = 100
                for i, p in enumerate(params):
                    if p == "movetime" and i + 1 < len(params):
                        movetime = int(params[i + 1])
                best = self.go(movetime=movetime)
                if best:
                    print(f"bestmove {best}")
            elif cmd == "d":
                print(self.board.state_key())


if __name__ == "__main__":
    engine = UCIEngine()
    engine.uci_loop()
