class_name PongSimulation
extends RefCounted
## Authoritative, seeded, fixed-step Pong-Breaker simulation. Coordinates are 360x640.
## Feed only the host's targets into step(); clients render apply_snapshot() state.

const FIXED_DT: float = 1.0 / 120.0
const WORLD_SIZE: Vector2 = Vector2(360, 640)
const LEFT: float = 6.0
const RIGHT: float = 354.0
const TOP_GOAL: float = 2.0
const BOTTOM_GOAL: float = 638.0
const PADDLE_Y: Array = [540.0, 100.0]
const PADDLE_WIDTH: float = 70.0
const WIDE_WIDTH: float = 110.0
const PADDLE_HEIGHT: float = 10.0
const BALL_RADIUS: float = 5.0
const BRICK_ORIGIN: Vector2 = Vector2(39, 263)
const MAX_BALLS: int = 6
const MATCH_SECONDS: float = 900.0
const POWER_KINDS: Array = ["WIDE", "SHIELD", "MULTIBALL", "OVERDRIVE", "STICKY"]
const EPS: float = 0.00001

var players: Array = []
var balls: Array = []
var bricks: Array = []
var drops: Array = []
var events: Array = []
var elapsed: float = 0.0
var phase: String = "playing"
var winner: int = -1
var spawned_bricks: int = 0
var _next_brick_id: int = 1
var serveTimer: float = 0.0
var flow_timer: float = 0.0
var feeders: Array = []
var drop_chance: float = 0.30
var tick: int = 0
var _accumulator: float = 0.0
var _next_ball_id: int = 1
var _next_drop_id: int = 1
var _seed_value: int = 1
var _tick_goals: Array = []
var _in_tick: bool = false
var _resolving_goals: bool = false
var _rng: RandomNumberGenerator = RandomNumberGenerator.new()


func _init() -> void:
	reset()


func reset(seed_value: int = 1) -> void:
	_seed_value = seed_value
	_rng.seed = seed_value
	elapsed = 0.0
	tick = 0
	_accumulator = 0.0
	phase = "playing"
	winner = -1
	spawned_bricks = 0
	_next_brick_id = 1
	serveTimer = 0.60
	flow_timer = 0.0
	feeders = [{"side": -1, "y": 270.0, "row": -1, "phase": "wait", "cooldown": 0.35, "progress": 0.0, "speed": 0.0},
		{"side": 1, "y": 370.0, "row": -1, "phase": "wait", "cooldown": 0.70, "progress": 0.0, "speed": 0.0}]
	_next_ball_id = 1
	_next_drop_id = 1
	drop_chance = 0.30
	events.clear()
	balls.clear()
	drops.clear()
	players.clear()
	for index in range(2):
		players.append({"x": 180.0, "hp": 5, "wide": 0.0, "shield": 0,
			"overdrive": 0.0, "sticky": 0.0, "score": 0, "velocity": 0.0, "aim": 0.0})
	_build_bricks()
	_spawn_ball(0, true, 0.60)
	_spawn_ball(1, true, 0.90)
	events.clear()


func paddle_width(player_index: int) -> float:
	return WIDE_WIDTH if float(players[player_index]["wide"]) > 0.0 else PADDLE_WIDTH


func paddle_rect(player_index: int) -> Rect2:
	var width: float = paddle_width(player_index)
	return Rect2(float(players[player_index]["x"]) - width * 0.5,
		float(PADDLE_Y[player_index]) - PADDLE_HEIGHT * 0.5, width, PADDLE_HEIGHT)


func serve_y(player_index: int) -> float:
	return float(PADDLE_Y[player_index]) + _inward(player_index) * (PADDLE_HEIGHT * 0.5 + BALL_RADIUS + 7.0)


func _inward(player_index: int) -> float:
	return -1.0 if player_index == 0 else 1.0


func step(delta: float, targets: Array) -> void:
	# No wall-clock reads or global randomness. No lost time from frame-rate clamps.
	events.clear()
	if phase != "playing" or not is_finite(delta) or delta <= 0.0:
		return
	_accumulator += delta
	while _accumulator + EPS * 0.001 >= FIXED_DT and phase == "playing":
		_accumulator = maxf(0.0, _accumulator - FIXED_DT)
		_tick(targets)


func _tick(targets: Array) -> void:
	tick += 1
	elapsed = float(tick) * FIXED_DT
	for index in range(2):
		var player: Dictionary = players[index]
		player["wide"] = maxf(0.0, float(player["wide"]) - FIXED_DT)
		player["overdrive"] = maxf(0.0, float(player["overdrive"]) - FIXED_DT)
		player["sticky"] = maxf(0.0, float(player["sticky"]) - FIXED_DT)
		var old_x: float = float(player["x"])
		var target: float = old_x
		if index < targets.size() and (targets[index] is float or targets[index] is int):
			target = float(targets[index])
		if not is_finite(target):
			target = old_x
		var half: float = paddle_width(index) * 0.5
		target = clampf(target, LEFT + half, RIGHT - half)
		# Touch targets are direct positions. Sweep old-to-new over this tick for collision timing.
		# AI rate limiting belongs to the caller, never to the human paddle.
		player["x"] = target
		player["velocity"] = (float(player["x"]) - old_x) / FIXED_DT
	_move_bricks()
	# Update every attachment before a goal can finish this tick halfway through the ball list.
	for ball in balls:
		if bool(ball["held"]):
			_position_held(ball)
			ball["hold_timer"] = maxf(0.0, float(ball["hold_timer"]) - FIXED_DT)
			if float(ball["hold_timer"]) <= EPS:
				_release_ball(ball)
	_tick_goals.clear()
	_in_tick = true
	var remove_ids: Array = []
	for ball_value in balls:
		var ball: Dictionary = ball_value
		_sync_ball_power(ball)
		if bool(ball["held"]):
			continue
		if float(ball["serve"]) > 0.0:
			ball["serve"] = maxf(0.0, float(ball["serve"]) - FIXED_DT)
			var owner: int = int(ball["owner"])
			ball["pos"] = Vector2(float(players[owner]["x"]), serve_y(owner))
			if float(ball["serve"]) <= EPS:
				ball["serve"] = 0.0
				events.append({"type": "serve", "ball": ball["id"], "owner": owner, "pos": ball["pos"]})
			continue
		if _move_ball(ball, FIXED_DT):
			remove_ids.append(ball["id"])
		if phase == "finished":
			break
	_in_tick = false
	_resolve_tick_goals(remove_ids)
	for index in range(balls.size() - 1, -1, -1):
		if balls[index]["id"] in remove_ids:
			balls.remove_at(index)
	if phase == "finished":
		return
	_move_drops()
	_tick_feeders()
	if tick >= int(MATCH_SECONDS / FIXED_DT):
		# A long match ends on lives, then brick score; an exact tie is a draw.
		var advantage: int = int(players[0].hp) - int(players[1].hp)
		if advantage == 0: advantage = int(players[0].score) - int(players[1].score)
		winner = 0 if advantage > 0 else 1 if advantage < 0 else -1
		phase = "finished"
		events.append({"type":"game_over", "winner":winner})
	serveTimer = 0.0
	for ball_value in balls:
		var delay: float = float(ball_value["serve"])
		if delay > 0.0 and (serveTimer == 0.0 or delay < serveTimer):
			serveTimer = delay


func _build_bricks() -> void:
	bricks.clear()
	for row in range(6):
		for column in range(7):
			var hp: int = 2 if (mini(column, 6 - column) + mini(row, 5 - row) + 1) % 4 == 0 else 1
			_add_brick(row, column, hp, false, -1 if column <= 3 else 1)


func _add_brick(row: int, column: int, hp: int, moving: bool, side: int) -> Dictionary:
	var target_x: float = BRICK_ORIGIN.x + float(column) * 41.0
	var x: float = (-36.0 if side < 0 else 360.0) if moving else target_x
	var brick: Dictionary = {"id": _next_brick_id, "rect": Rect2(x, BRICK_ORIGIN.y + row * 20, 36, 14),
		"hp": hp, "max_hp": hp, "row": row, "column": column, "origin_side": side,
		"target_x": target_x, "moving": moving, "speed": 60.0 if moving else 0.0, "style": hp - 1}
	_next_brick_id += 1
	bricks.append(brick)
	return brick


func _row_gap(row: int, side: int) -> int:
	for offset in range(4):
		var column: int = offset if side < 0 else 6 - offset
		var occupied: bool = false
		for brick in bricks:
			if int(brick.get("row", -1)) == row and int(brick.get("column", -1)) == column:
				occupied = true
				break
		if not occupied:
			return column
	return -1


func _reserve_feeder(feeder: Dictionary) -> void:
	if bricks.size() >= 42:
		return
	var candidates: Array = []
	var shortest: float = INF
	for row in range(6):
		var reserved: bool = false
		for other in feeders:
			reserved = reserved or (String(other["phase"]) != "wait" and int(other["row"]) == row)
		if reserved or _row_gap(row, int(feeder["side"])) < 0:
			continue
		var travel: float = absf(float(feeder["y"]) - (270.0 + row * 20.0))
		if bricks.size() < 16 and travel < shortest - EPS:
			candidates.clear()
			shortest = travel
		if bricks.size() >= 16 or travel <= shortest + EPS:
			candidates.append(row)
	if candidates.is_empty():
		return
	feeder["row"] = int(candidates[_rng.randi_range(0, candidates.size() - 1)])
	feeder["phase"] = "travel"
	feeder["cooldown"] = 0.0
	feeder["progress"] = 0.0
	feeder["speed"] = 0.0


func _start_push(feeder: Dictionary) -> void:
	var row: int = int(feeder["row"])
	var side: int = int(feeder["side"])
	var gap: int = _row_gap(row, side)
	if gap < 0 or bricks.size() >= 42:
		_finish_push(feeder)
		return
	feeder["speed"] = 36.0 if bricks.size() < 16 else 26.0
	feeder["phase"] = "push"
	feeder["progress"] = 0.0
	# Shift only the contiguous edge chain by one grid pitch. The first inner gap
	# receives its adjacent old brick; the newly manufactured brick stays at the edge.
	for brick in bricks:
		if int(brick.get("row", -1)) != row:
			continue
		var column: int = int(brick["column"])
		if (side < 0 and column < gap) or (side > 0 and column > gap):
			brick["column"] = column - side
			brick["target_x"] = BRICK_ORIGIN.x + float(brick["column"]) * 41.0
			brick["origin_side"] = side
			brick["moving"] = true
			brick["speed"] = float(feeder["speed"])
	var roll: int = _rng.randi_range(0, 9)
	var hp: int = 3 if roll == 9 else (2 if roll >= 6 else 1)
	var brick: Dictionary = _add_brick(row, 0 if side < 0 else 6, hp, true, side)
	brick["rect"] = Rect2(-2.0 if side < 0 else 326.0, 263.0 + row * 20.0, 36, 14)
	brick["speed"] = float(feeder["speed"])
	spawned_bricks += 1
	events.append({"type": "brick_spawned", "brick": brick["id"], "side": side, "pos": Rect2(brick["rect"]).get_center()})


func _finish_push(feeder: Dictionary) -> void:
	feeder["phase"] = "wait"
	feeder["row"] = -1
	feeder["progress"] = 0.0
	feeder["speed"] = 0.0
	feeder["cooldown"] = 0.10 if bricks.size() < 16 else 0.55


func _feed_brick() -> void:
	# Convenience for fixtures: normal simulation advances travel before pushing.
	for feeder in feeders:
		if String(feeder["phase"]) == "wait":
			_reserve_feeder(feeder)


func _tick_feeders() -> void:
	for feeder in feeders:
		match String(feeder["phase"]):
			"wait":
				feeder["cooldown"] = maxf(0.0, float(feeder["cooldown"]) - FIXED_DT)
				if float(feeder["cooldown"]) <= EPS:
					_reserve_feeder(feeder)
			"travel":
				var target: float = 270.0 + float(feeder["row"]) * 20.0
				feeder["y"] = move_toward(float(feeder["y"]), target, 96.0 * FIXED_DT)
				if absf(float(feeder["y"]) - target) <= EPS:
					_start_push(feeder)
	# Retained as a read-only legacy diagnostic; per-feeder clocks drive the flow.
	flow_timer = minf(float(feeders[0]["cooldown"]), float(feeders[1]["cooldown"]))


func _move_bricks() -> void:
	for brick in bricks:
		brick["speed"] = 0.0
	for feeder in feeders:
		if String(feeder["phase"]) != "push":
			continue
		feeder["progress"] = minf(1.0, float(feeder["progress"]) + float(feeder["speed"]) * FIXED_DT / 41.0)
		for brick in bricks:
			if int(brick.get("row", -1)) != int(feeder["row"]) or not bool(brick.get("moving", false)):
				continue
			var rect: Rect2 = brick["rect"]
			var old_x: float = rect.position.x
			rect.position.x = float(brick["target_x"]) + int(feeder["side"]) * 41.0 * (1.0 - float(feeder["progress"]))
			brick["rect"] = rect
			brick["speed"] = absf(rect.position.x - old_x) / FIXED_DT
			if float(feeder["progress"]) >= 1.0:
				brick["moving"] = false
				events.append({"type": "brick_arrived", "brick": brick["id"], "side": brick["origin_side"], "pos": rect.get_center()})
		if float(feeder["progress"]) >= 1.0:
			_finish_push(feeder)


func _brick_velocity(brick: Dictionary) -> Vector2:
	return Vector2(-int(brick.get("origin_side", 0)) * float(brick.get("speed", 0.0)), 0.0)


func shield_y(player_index: int, charges: int = -1) -> float:
	var count: int = int(players[player_index]["shield"]) if charges < 0 else charges
	var bottom: float = 570.0 + 18.0 * float(3 - clampi(count, 1, 3))
	return bottom if player_index == 0 else WORLD_SIZE.y - bottom


func _position_held(ball: Dictionary) -> void:
	var owner: int = int(ball["owner"])
	var half: float = paddle_width(owner) * 0.5 - BALL_RADIUS
	ball["hold_offset"] = clampf(float(ball["hold_offset"]), -half, half)
	ball["pos"] = Vector2(float(players[owner]["x"]) + float(ball["hold_offset"]), float(PADDLE_Y[owner]) + _inward(owner) * (PADDLE_HEIGHT * 0.5 + BALL_RADIUS + 0.03))


func set_aim(player_index: int, aim: float) -> void:
	if player_index >= 0 and player_index < 2 and is_finite(aim):
		players[player_index]["aim"] = clampf(aim, -1.0, 1.0)


func release_sticky(player_index: int, aim: float = NAN) -> int:
	if phase != "playing" or player_index < 0 or player_index > 1:
		return 0
	set_aim(player_index, aim)
	var released: int = 0
	for ball in balls:
		if bool(ball["held"]) and int(ball["owner"]) == player_index:
			_position_held(ball)
			_release_ball(ball)
			released += 1
	return released


func _release_ball(ball: Dictionary) -> void:
	var owner: int = int(ball["owner"])
	var angle: float = float(players[owner]["aim"]) * 1.04
	ball["vel"] = Vector2(sin(angle), _inward(owner) * cos(angle)) * float(ball["speed"])
	ball["held"] = false
	ball["hold_timer"] = 0.0
	ball["hold_offset"] = 0.0
	ball["hold_kind"] = ""
	_sync_ball_power(ball)
	events.append({"type": "sticky_release", "ball": ball["id"], "owner": owner, "pos": ball["pos"]})


func _spawn_ball(owner: int, base: bool, delay: float) -> Dictionary:
	var ball: Dictionary = {"id": _next_ball_id, "pos": Vector2.ZERO, "vel": Vector2.ZERO,
		"owner": owner, "radius": BALL_RADIUS, "base": base, "serve": delay,
		"speed": 240.0, "overdrive": false, "damage": 1, "hits": 0, "held": false, "hold_timer": 0.0, "hold_offset": 0.0, "hold_kind": ""}
	_next_ball_id += 1
	_prepare_serve(ball, owner, delay)
	balls.append(ball)
	return ball


func _prepare_serve(ball: Dictionary, owner: int, delay: float) -> void:
	ball["owner"] = owner
	ball["serve"] = delay
	ball["held"] = false
	ball["hold_timer"] = 0.0
	ball["hold_offset"] = 0.0
	ball["hold_kind"] = ""
	ball["pos"] = Vector2(float(players[owner]["x"]), serve_y(owner))
	ball["speed"] = 240.0
	ball["overdrive"] = false
	ball["damage"] = 1
	var angle: float = _rng.randf_range(-0.34, 0.34)
	ball["vel"] = Vector2(sin(angle), -cos(angle) if owner == 0 else cos(angle)) * 240.0
	_sync_ball_power(ball)


func _sync_ball_power(ball: Dictionary) -> void:
	var powered: bool = float(players[int(ball["owner"])]["overdrive"]) > 0.0
	ball["overdrive"] = powered
	ball["damage"] = 2 if powered else 1
	var speed: float = maxf(350.0, float(ball["speed"])) if powered else clampf(float(ball["speed"]), 180.0, 400.0)
	var velocity: Vector2 = ball["vel"]
	if velocity.length_squared() > EPS:
		ball["vel"] = velocity.normalized() * speed


func _move_ball(ball: Dictionary, duration: float) -> bool:
	var remaining: float = duration
	var resolved_paddles: Array = []
	var resolved_bricks: Array = []
	for _bounce in range(8):
		if remaining <= EPS or phase != "playing":
			break
		var start: Vector2 = ball["pos"]
		var velocity: Vector2 = ball["vel"]
		var motion: Vector2 = velocity * remaining
		var radius: float = float(ball["radius"])
		var best: Dictionary = {"t": 2.0}
		if motion.x < -EPS:
			_consider(best, (LEFT + radius - start.x) / motion.x, "wall", Vector2.RIGHT)
		elif motion.x > EPS:
			_consider(best, (RIGHT - radius - start.x) / motion.x, "wall", Vector2.LEFT)
		if motion.y < -EPS:
			_consider(best, (TOP_GOAL - start.y) / motion.y, "goal", Vector2.DOWN, 1)
		elif motion.y > EPS:
			_consider(best, (BOTTOM_GOAL - start.y) / motion.y, "goal", Vector2.UP, 0)
		for player_index in range(2):
			if int(players[player_index]["shield"]) > 0 and motion.y * _inward(player_index) < -EPS:
				# A pickup may add an outer plane behind a ball already approaching an
				# existing inner plane. Sweep every remaining plane; _consider keeps
				# the first future crossing and rejects all already-passed planes.
				for charge in range(1, int(players[player_index]["shield"]) + 1):
					var plane: float = shield_y(player_index, charge) + _inward(player_index) * radius
					_consider(best, (plane - start.y) / motion.y, "shield", Vector2(0, _inward(player_index)), player_index)
			if player_index in resolved_paddles:
				continue
			# Relative motion is continuous even for a full-width swipe in a single tick.
			var paddle_velocity: float = float(players[player_index]["velocity"])
			var moving_rect: Rect2 = paddle_rect(player_index)
			moving_rect.position.x -= paddle_velocity * remaining
			var relative_motion: Vector2 = motion - Vector2(paddle_velocity * remaining, 0.0)
			var hit: Dictionary = _sweep_circle_rect(start, relative_motion, moving_rect, radius)
			if hit.is_empty():
				continue
			var incoming: bool = velocity.y * _inward(player_index) < -EPS
			if bool(hit.get("overlap", false)):
				# A widened paddle or restored state may begin overlapped. Outgoing balls
				# are separated without awarding another hit or changing ownership.
				var approaching: bool = relative_motion.dot(Vector2(hit["normal"])) < -EPS
				var kind: String = "paddle" if incoming and approaching else "paddle_separate"
				_consider(best, 0.0, kind, hit["normal"], player_index)
				if String(best.get("kind", "")) == "paddle_separate" and int(best["index"]) == player_index:
					best["depth"] = hit["depth"]
			elif incoming:
				_consider(best, float(hit["t"]), "paddle", hit["normal"], player_index)
		for index in range(bricks.size()):
			if bricks[index]["id"] in resolved_bricks:
				continue
			var rect: Rect2 = bricks[index]["rect"]
			var brick_velocity: Vector2 = _brick_velocity(bricks[index])
			rect.position -= brick_velocity * remaining
			var hit: Dictionary = _sweep_exposed_brick(start, motion, rect, brick_velocity * remaining, radius, remaining)
			if not hit.is_empty():
				_consider(best, float(hit["t"]), "brick", hit["normal"], index)
				if String(best.get("kind", "")) == "brick" and int(best["index"]) == index:
					best["depth"] = float(hit.get("depth", 0.0))
					best["surface_velocity"] = hit.get("surface_velocity", brick_velocity)
		if float(best["t"]) > 1.0:
			ball["pos"] = start + motion
			break
		var fraction: float = clampf(float(best["t"]), 0.0, 1.0)
		ball["pos"] = start + motion * fraction
		remaining *= 1.0 - fraction
		var normal: Vector2 = best["normal"]
		match String(best["kind"]):
			"goal":
				if _in_tick:
					_tick_goals.append({"ball":ball, "defender":int(best["index"]), "time":duration - remaining})
					return false
				return _goal(ball, int(best["index"]))
			"shield":
				var defender: int = int(best["index"])
				players[defender]["shield"] = int(players[defender]["shield"]) - 1
				ball["owner"] = defender
				ball["vel"] = velocity.bounce(normal)
				_sync_ball_power(ball)
				events.append({"type": "shield_block", "player": defender, "ball": ball["id"], "pos": ball["pos"]})
			"paddle":
				var player_index: int = int(best["index"])
				var impact_x: float = float(players[player_index]["x"]) - float(players[player_index]["velocity"]) * remaining
				_paddle_hit(ball, player_index, impact_x)
				_separate_from_paddle(ball, player_index)
				if bool(ball["held"]):
					_position_held(ball)
					return false
				resolved_paddles.append(player_index)
				normal = Vector2(0.0, _inward(player_index))
			"paddle_separate":
				var player_index: int = int(best["index"])
				_separate_overlap(ball, player_index, normal, float(best["depth"]))
				resolved_paddles.append(player_index)
				normal = Vector2.ZERO
			"brick":
				var index: int = int(best["index"])
				var brick_velocity: Vector2 = best.get("surface_velocity", _brick_velocity(bricks[index]))
				resolved_bricks.append(bricks[index]["id"])
				var separated: Vector2 = Vector2(ball["pos"]) + normal * float(best.get("depth", 0.0))
				if separated.x < LEFT + radius or separated.x > RIGHT - radius:
					# At an entering brick's rear corner, horizontal depenetration can point
					# through the side wall. Use the nearest free vertical face instead.
					var impact_rect: Rect2 = bricks[index]["rect"]
					impact_rect.position -= brick_velocity * remaining
					var vertical: float = signf(Vector2(ball["pos"]).y - impact_rect.get_center().y)
					if absf(vertical) < EPS:
						vertical = _inward(int(ball["owner"]))
					normal = Vector2(0.0, vertical)
					separated.x = clampf(Vector2(ball["pos"]).x, LEFT + radius, RIGHT - radius)
					separated.y = impact_rect.position.y - radius if vertical < 0.0 else impact_rect.end.y + radius
				ball["pos"] = separated
				var reflected: Vector2 = (velocity - brick_velocity).bounce(normal) + brick_velocity
				# Rounded corners can produce an almost horizontal orbit. Keep rallies progressing.
				var length: float = reflected.length()
				if absf(reflected.y) < length * 0.20:
					var vertical: float = signf(reflected.y) if absf(reflected.y) > EPS else _inward(int(ball["owner"]))
					reflected.y = vertical * length * 0.20
					reflected.x = signf(reflected.x) * sqrt(maxf(0.0, length * length - reflected.y * reflected.y))
				ball["vel"] = reflected
				_sync_ball_power(ball)
				_hit_brick(ball, index)
			"wall":
				ball["vel"] = velocity.bounce(normal)
				events.append({"type": "wall_hit", "ball": ball["id"], "pos": ball["pos"]})
		ball["pos"] = Vector2(ball["pos"]) + normal * 0.01
	return false


func _consider(best: Dictionary, fraction: float, kind: String, normal: Vector2, index: int = -1) -> void:
	if fraction >= -EPS and fraction <= 1.0 and fraction < float(best["t"]) - EPS:
		best["t"] = maxf(0.0, fraction)
		best["kind"] = kind
		best["normal"] = normal
		best["index"] = index


func _sweep_rect(start: Vector2, motion: Vector2, rect: Rect2) -> Dictionary:
	var near_time: float = -INF
	var far_time: float = INF
	var normal: Vector2 = Vector2.ZERO
	for axis in range(2):
		var origin: float = start[axis]
		var distance: float = motion[axis]
		var lower: float = rect.position[axis]
		var upper: float = rect.end[axis]
		if absf(distance) <= EPS:
			if origin < lower or origin > upper:
				return {}
			continue
		var near_axis: float = (lower - origin) / distance
		var far_axis: float = (upper - origin) / distance
		var face: float = -1.0
		if near_axis > far_axis:
			var swap: float = near_axis
			near_axis = far_axis
			far_axis = swap
			face = 1.0
		if near_axis > near_time:
			near_time = near_axis
			normal = Vector2.ZERO
			normal[axis] = face
		far_time = minf(far_time, far_axis)
		if near_time > far_time:
			return {}
	if near_time < -EPS or near_time > 1.0 or far_time < 0.0:
		return {}
	return {"t": maxf(0.0, near_time), "normal": normal}


func _sweep_circle_rect(start: Vector2, motion: Vector2, rect: Rect2, radius: float) -> Dictionary:
	# Minkowski sum of a rectangle and a circle: four face strips plus four round corners.
	# Merely growing the AABB invents collisions in the square corner regions.
	var expanded: Rect2 = rect.grow(radius)
	if not expanded.has_point(start) and _sweep_rect(start, motion, expanded).is_empty():
		return {}
	var closest: Vector2 = Vector2(clampf(start.x, rect.position.x, rect.end.x), clampf(start.y, rect.position.y, rect.end.y))
	var offset: Vector2 = start - closest
	var distance_squared: float = offset.length_squared()
	if distance_squared < radius * radius - EPS:
		var normal: Vector2
		var depth: float
		if distance_squared > EPS:
			normal = offset.normalized()
			depth = radius - sqrt(distance_squared)
		else:
			# Centre inside the rectangle: select the closest actual face deterministically.
			var distances: Array = [start.x - rect.position.x, rect.end.x - start.x,
				start.y - rect.position.y, rect.end.y - start.y]
			var normals: Array = [Vector2.LEFT, Vector2.RIGHT, Vector2.UP, Vector2.DOWN]
			var selected: int = 0
			for index in range(1, 4):
				if float(distances[index]) < float(distances[selected]) - EPS or (absf(float(distances[index]) - float(distances[selected])) <= EPS and motion.dot(Vector2(normals[index])) < motion.dot(Vector2(normals[selected]))):
					selected = index
			normal = normals[selected]
			depth = radius + float(distances[selected])
		return {"t": 0.0, "normal": normal, "overlap": true, "depth": depth}
	var best: Dictionary = {"t": 2.0}
	for axis in range(2):
		if absf(motion[axis]) <= EPS:
			continue
		var other: int = 1 - axis
		var face: float = rect.position[axis] - radius if motion[axis] > 0.0 else rect.end[axis] + radius
		var fraction: float = (face - start[axis]) / motion[axis]
		var cross: float = start[other] + motion[other] * fraction
		if cross >= rect.position[other] - EPS and cross <= rect.end[other] + EPS:
			var normal: Vector2 = Vector2.ZERO
			normal[axis] = -1.0 if motion[axis] > 0.0 else 1.0
			_consider(best, fraction, "face", normal)
	var length_squared: float = motion.length_squared()
	if length_squared > EPS:
		for horizontal in range(2):
			for vertical in range(2):
				var corner: Vector2 = Vector2(rect.position.x if horizontal == 0 else rect.end.x,
					rect.position.y if vertical == 0 else rect.end.y)
				var from_corner: Vector2 = start - corner
				var projected: float = from_corner.dot(motion)
				var discriminant: float = projected * projected - length_squared * (from_corner.length_squared() - radius * radius)
				if discriminant < 0.0:
					continue
				var fraction: float = (-projected - sqrt(discriminant)) / length_squared
				if fraction < -EPS or fraction > 1.0:
					continue
				var point: Vector2 = start + motion * maxf(0.0, fraction)
				if (horizontal == 0 and point.x > corner.x + EPS) or (horizontal == 1 and point.x < corner.x - EPS):
					continue
				if (vertical == 0 and point.y > corner.y + EPS) or (vertical == 1 and point.y < corner.y - EPS):
					continue
				var normal: Vector2 = (point - corner).normalized()
				if motion.dot(normal) < -EPS:
					_consider(best, fraction, "corner", normal)
	if float(best["t"]) > 1.0:
		return {}
	return {"t": float(best["t"]), "normal": best["normal"], "overlap": false}


func _sweep_exposed_brick(start: Vector2, motion: Vector2, rect: Rect2, travel: Vector2, radius: float, duration: float) -> Dictionary:
	# Only material outside the foreground machine can hit a ball. Split at mouth
	# crossings, then solve the growing visible rectangle continuously, including corners.
	if rect.position.x >= 39.0 and rect.end.x <= 321.0 and rect.position.x + travel.x >= 39.0 and rect.end.x + travel.x <= 321.0:
		return _sweep_circle_rect(start, motion - travel, rect, radius)
	var times: Array = [0.0, 1.0]
	if absf(travel.x) > EPS:
		for edge in [rect.position.x, rect.end.x]:
			for mouth in [39.0, 321.0]:
				var time: float = (mouth - float(edge)) / travel.x
				if time > EPS and time < 1.0 - EPS:
					times.append(time)
	times.sort()
	for index in range(times.size() - 1):
		var lower: float = float(times[index])
		var span: float = float(times[index + 1]) - lower
		if span <= EPS:
			continue
		var middle_x: float = rect.position.x + travel.x * (lower + span * 0.5)
		if minf(middle_x + rect.size.x,321.0) <= maxf(middle_x,39.0) + EPS:
			continue
		var left: float = maxf(rect.position.x + travel.x * lower, 39.0)
		var right: float = minf(rect.end.x + travel.x * lower, 321.0)
		var end_left: float = maxf(rect.position.x + travel.x * (lower + span),39.0)
		var end_right: float = minf(rect.end.x + travel.x * (lower + span),321.0)
		var visible: Rect2 = Rect2(left,rect.position.y,maxf(0.0,right-left),rect.size.y)
		var hit: Dictionary = _sweep_changing_rect(start + motion * lower, motion * span, visible, end_left-left, end_right-right, radius, duration * span)
		if not hit.is_empty():
			hit["t"] = lower + span * float(hit["t"])
			return hit
	return {}


func _sweep_changing_rect(start: Vector2, motion: Vector2, rect: Rect2, left_travel: float, right_travel: float, radius: float, duration: float) -> Dictionary:
	var initial: Dictionary = _sweep_circle_rect(start, Vector2.ZERO, rect, radius)
	if not initial.is_empty() and bool(initial.get("overlap",false)):
		var horizontal: float = Vector2(initial["normal"]).x
		initial["surface_velocity"] = Vector2((left_travel if horizontal < 0 else right_travel) / duration,0) if absf(horizontal) > EPS else Vector2.ZERO
		return initial
	var best: Dictionary = {"t":2.0}
	for horizontal in range(2):
		var normal: Vector2 = Vector2.LEFT if horizontal == 0 else Vector2.RIGHT
		var edge: float = rect.position.x if horizontal == 0 else rect.end.x
		var edge_travel: float = left_travel if horizontal == 0 else right_travel
		var relative: float = motion.x - edge_travel
		if relative * normal.x >= -EPS:
			continue
		var time: float = (edge + normal.x * radius - start.x) / relative
		var y: float = start.y + motion.y * time
		if y >= rect.position.y-EPS and y <= rect.end.y+EPS and time >= -EPS and time <= 1.0 and time < float(best["t"]):
			best = {"t":maxf(0.0,time),"normal":normal,"surface_velocity":Vector2(edge_travel/duration,0)}
	if absf(motion.y) > EPS:
		var normal: Vector2 = Vector2.UP if motion.y > 0 else Vector2.DOWN
		var edge: float = rect.position.y if motion.y > 0 else rect.end.y
		var time: float = (edge + normal.y * radius - start.y) / motion.y
		var x: float = start.x + motion.x * time
		if x >= rect.position.x+left_travel*time-EPS and x <= rect.end.x+right_travel*time+EPS and time >= -EPS and time <= 1.0 and time < float(best["t"]):
			best = {"t":maxf(0.0,time),"normal":normal,"surface_velocity":Vector2.ZERO}
	for horizontal in range(2):
		var edge_travel: float = left_travel if horizontal == 0 else right_travel
		var relative: Vector2 = motion - Vector2(edge_travel,0)
		var length_squared: float = relative.length_squared()
		if length_squared <= EPS:
			continue
		for vertical in range(2):
			var corner: Vector2 = Vector2(rect.position.x if horizontal == 0 else rect.end.x,rect.position.y if vertical == 0 else rect.end.y)
			var offset: Vector2 = start-corner
			var projected: float = offset.dot(relative)
			var discriminant: float = projected*projected-length_squared*(offset.length_squared()-radius*radius)
			if discriminant < 0:
				continue
			var time: float = (-projected-sqrt(discriminant))/length_squared
			if time < -EPS or time > 1.0 or time >= float(best["t"]):
				continue
			var delta: Vector2 = offset + relative * maxf(0.0,time)
			if (horizontal == 0 and delta.x > EPS) or (horizontal == 1 and delta.x < -EPS) or (vertical == 0 and delta.y > EPS) or (vertical == 1 and delta.y < -EPS):
				continue
			var normal: Vector2 = delta.normalized()
			if relative.dot(normal) < -EPS:
				best = {"t":maxf(0.0,time),"normal":normal,"surface_velocity":Vector2(edge_travel/duration,0)}
	return best if float(best["t"]) <= 1.0 else {}


func _separate_from_paddle(ball: Dictionary, player_index: int) -> void:
	# Arcade paddle returns always leave toward the court, including a side swipe.
	# Place the whole circle beyond that face so a fast paddle cannot swallow it again.
	var radius: float = float(ball["radius"])
	var pos: Vector2 = ball["pos"]
	pos.y = float(PADDLE_Y[player_index]) + _inward(player_index) * (PADDLE_HEIGHT * 0.5 + radius + 0.02)
	pos.x = clampf(pos.x, LEFT + radius, RIGHT - radius)
	ball["pos"] = pos


func _separate_overlap(ball: Dictionary, player_index: int, normal: Vector2, depth: float) -> void:
	# An already-separating back/side overlap must not be teleported onto the return face:
	# that would turn a legitimate miss into a phantom save on the next tick.
	var radius: float = float(ball["radius"])
	var pos: Vector2 = Vector2(ball["pos"]) + normal * (depth + 0.03)
	if pos.x < LEFT + radius or pos.x > RIGHT - radius:
		# A side wall can make horizontal depenetration impossible. Escape vertically
		# in the ball's existing travel direction without adding energy or awarding a hit.
		var direction: float = -1.0 if Vector2(ball["vel"]).y < 0.0 else 1.0
		pos.x = clampf(pos.x, LEFT + radius, RIGHT - radius)
		pos.y = float(PADDLE_Y[player_index]) + direction * (PADDLE_HEIGHT * 0.5 + radius + 0.03)
	ball["pos"] = pos


func _paddle_hit(ball: Dictionary, player_index: int, impact_x: float) -> void:
	var offset: float = clampf((Vector2(ball["pos"]).x - impact_x) / (paddle_width(player_index) * 0.5), -1.0, 1.0)
	ball["owner"] = player_index
	ball["hits"] = int(ball["hits"]) + 1
	ball["speed"] = minf(305.0, float(ball["speed"]) + 7.0)
	var english: float = clampf(float(players[player_index]["velocity"]) * 0.00015, -0.14, 0.14)
	var angle: float = clampf(offset * 1.02 + english, -1.04, 1.04)
	var direction: Vector2 = Vector2(sin(angle), -cos(angle) if player_index == 0 else cos(angle))
	# Even an instantaneous full-court finger swipe keeps at least half the speed vertical.
	ball["vel"] = direction.normalized() * float(ball["speed"])
	_sync_ball_power(ball)
	events.append({"type": "paddle_hit", "ball": ball["id"], "owner": player_index, "pos": ball["pos"]})
	if float(players[player_index]["sticky"]) > 0.0:
		ball["held"] = true
		ball["hold_timer"] = 3.6
		ball["hold_kind"] = "sticky"
		ball["hold_offset"] = clampf(Vector2(ball["pos"]).x - impact_x, -paddle_width(player_index) * 0.5 + BALL_RADIUS, paddle_width(player_index) * 0.5 - BALL_RADIUS)
		events.append({"type": "sticky_catch", "ball": ball["id"], "owner": player_index, "pos": ball["pos"]})


func _hit_brick(ball: Dictionary, index: int) -> void:
	var brick: Dictionary = bricks[index]
	var damage: int = mini(int(brick["hp"]), int(ball["damage"]))
	brick["hp"] = int(brick["hp"]) - damage
	var owner: int = int(ball["owner"])
	players[owner]["score"] = int(players[owner]["score"]) + damage * 10
	var pos: Vector2 = Rect2(brick["rect"]).get_center()
	events.append({"type": "brick_hit", "brick": brick["id"], "owner": owner, "pos": pos, "hp": brick["hp"]})
	if int(brick["hp"]) <= 0:
		players[owner]["score"] = int(players[owner]["score"]) + 25
		events.append({"type": "brick_destroyed", "brick": brick["id"], "owner": owner, "pos": pos})
		bricks.remove_at(index)
		if _rng.randf() < drop_chance:
			var kind: String = String(POWER_KINDS[_rng.randi_range(0, POWER_KINDS.size() - 1)])
			pos.x = clampf(pos.x, LEFT + 7.0, RIGHT - 7.0)
			var drop: Dictionary = {"id": _next_drop_id, "pos": pos, "owner": owner, "kind": kind}
			_next_drop_id += 1
			drops.append(drop)
			events.append({"type": "drop_spawned", "id": drop["id"], "owner": owner, "kind": kind, "pos": pos})


func _resolve_tick_goals(remove_ids: Array) -> void:
	# Resolve goal crossings by their swept time, never by entity insertion order.
	# Crossings within 10 microseconds are simultaneous; two fatal goals draw.
	_tick_goals.sort_custom(func(a, b): return float(a.time) < float(b.time))
	_resolving_goals = true
	var first: int = 0
	while first < _tick_goals.size() and phase == "playing":
		var end: int = first + 1
		while end < _tick_goals.size() and float(_tick_goals[end].time) - float(_tick_goals[first].time) <= EPS:
			end += 1
		for index in range(first, end):
			var goal: Dictionary = _tick_goals[index]
			if _goal(goal.ball, int(goal.defender)): remove_ids.append(goal.ball.id)
		if int(players[0].hp) == 0 or int(players[1].hp) == 0:
			winner = -1 if int(players[0].hp) == int(players[1].hp) else 0 if int(players[1].hp) == 0 else 1
			phase = "finished"
			events.append({"type":"game_over", "winner":winner})
		first = end
	_resolving_goals = false
	_tick_goals.clear()


func _goal(ball: Dictionary, defender: int) -> bool:
	var player: Dictionary = players[defender]
	player["hp"] = maxi(0, int(player["hp"]) - 1)
	var attacker: int = int(ball["owner"])
	if attacker != defender:
		players[attacker]["score"] = int(players[attacker]["score"]) + 100
	events.append({"type": "goal", "player": defender, "defender": defender, "owner": attacker,
		"ball": ball["id"], "hp": player["hp"], "pos": ball["pos"]})
	if int(player["hp"]) <= 0 and not _resolving_goals:
		phase = "finished"
		winner = 1 - defender
		events.append({"type": "game_over", "winner": winner})
		return false
	if bool(ball["base"]):
		if int(player["hp"]) > 0: _prepare_serve(ball, defender, 0.75)
		return false
	return true


func _move_drops() -> void:
	for index in range(drops.size() - 1, -1, -1):
		var drop: Dictionary = drops[index]
		var owner: int = int(drop["owner"])
		var start: Vector2 = drop["pos"]
		var motion: Vector2 = Vector2(0.0, -_inward(owner) * 94.0 * FIXED_DT)
		var pos: Vector2 = start + motion
		drop["pos"] = pos
		var moving_rect: Rect2 = paddle_rect(owner)
		var paddle_motion: Vector2 = Vector2(float(players[owner]["velocity"]) * FIXED_DT, 0.0)
		moving_rect.position -= paddle_motion
		if not _sweep_circle_rect(start, motion - paddle_motion, moving_rect, 7.0).is_empty():
			_grant_powerup(owner, String(drop["kind"]))
			drops.remove_at(index)
		elif (pos.y - float(PADDLE_Y[owner])) * -_inward(owner) > 24.0:
			drops.remove_at(index)


func _grant_powerup(player_index: int, kind: String) -> void:
	var player: Dictionary = players[player_index]
	var capped: bool = (kind == "SHIELD" and int(player.shield) >= 3) or (kind == "MULTIBALL" and balls.size() >= MAX_BALLS)
	if capped:
		events.append({"type":"powerup_capped", "player":player_index, "owner":player_index, "kind":kind, "pos":Vector2(float(player.x), float(PADDLE_Y[player_index]))})
		return
	match kind:
		"WIDE":
			player["wide"] = 12.0
			player["x"] = clampf(float(player["x"]), LEFT + WIDE_WIDTH * 0.5, RIGHT - WIDE_WIDTH * 0.5)
			for ball in balls:
				if bool(ball["held"]) and int(ball["owner"]) == player_index:
					_position_held(ball)
		"SHIELD":
			player["shield"] = mini(3, int(player["shield"]) + 1)
		"STICKY":
			player["sticky"] = 12.0
		"OVERDRIVE":
			player["overdrive"] = 8.0
			for ball_value in balls:
				if int(ball_value["owner"]) == player_index:
					_sync_ball_power(ball_value)
		"MULTIBALL":
			if balls.size() < MAX_BALLS:
				var extra: Dictionary = _spawn_ball(player_index, false, 0.0)
				extra["held"] = true
				extra["hold_timer"] = 3.6
				extra["hold_kind"] = "bonus"
				extra["hold_offset"] = 0.0
				_position_held(extra)
				_sync_ball_power(extra)
		_:
			return
	events.append({"type": "powerup", "player": player_index, "owner": player_index, "kind": kind,
		"pos": Vector2(float(player["x"]), float(PADDLE_Y[player_index]))})


func get_ai_target(player_index: int, difficulty: float = 0.75) -> float:
	# Predict the closest incoming ball with side-wall folding; uncertainty is periodic, never random.
	if player_index < 0 or player_index > 1:
		return 180.0
	var earliest: float = INF
	var target: float = 180.0
	for ball_value in balls:
		if float(ball_value["serve"]) > 0.0 or bool(ball_value["held"]):
			continue
		var pos: Vector2 = ball_value["pos"]
		var velocity: Vector2 = ball_value["vel"]
		if (player_index == 0 and velocity.y <= 0.0) or (player_index == 1 and velocity.y >= 0.0):
			continue
		var contact_y: float = float(PADDLE_Y[player_index]) + (-10.0 if player_index == 0 else 10.0)
		var arrival: float = (contact_y - pos.y) / velocity.y
		if arrival < 0.0 or arrival >= earliest:
			continue
		earliest = arrival
		var span: float = RIGHT - LEFT - BALL_RADIUS * 2.0
		var folded: float = fposmod(pos.x + velocity.x * arrival - LEFT - BALL_RADIUS, span * 2.0)
		target = LEFT + BALL_RADIUS + (folded if folded <= span else span * 2.0 - folded)
	var skill: float = clampf(difficulty, 0.0, 1.0)
	if earliest > 1.0:
		# Catch a useful drop only when no imminent save takes priority.
		for drop_value in drops:
			if int(drop_value["owner"]) == player_index:
				target = Vector2(drop_value["pos"]).x
				break
	target += sin(elapsed * 1.71 + float(player_index) * 2.3) * (1.0 - skill) * 55.0
	var half: float = paddle_width(player_index) * 0.5
	return clampf(target, LEFT + half, RIGHT - half)


func snapshot() -> Dictionary:
	return {"version": 3, "feeders": feeders.duplicate(true), "players": players.duplicate(true), "balls": balls.duplicate(true),
		"bricks": bricks.duplicate(true), "drops": drops.duplicate(true), "elapsed": elapsed,
		"phase": phase, "winner": winner, "spawned_bricks": spawned_bricks, "serveTimer": serveTimer,
		"flow_timer": flow_timer, "next_brick_id": _next_brick_id, "tick": tick, "accumulator": _accumulator,
		"seed": _seed_value, "rng_state": _rng.state, "next_ball_id": _next_ball_id,
		"next_drop_id": _next_drop_id, "drop_chance": drop_chance}


static func valid_snapshot(state: Dictionary) -> bool:
	# Network boundary: validate completely before the UI, renderer or simulation sees it.
	# This checks representation and resource bounds, not whether an authoritative host is fair.
	var required: Array = ["version", "players", "balls", "bricks", "drops", "elapsed", "phase",
		"winner", "spawned_bricks", "serveTimer", "flow_timer", "next_brick_id", "tick", "accumulator", "seed", "rng_state",
		"next_ball_id", "next_drop_id", "drop_chance", "feeders"]
	if not _snapshot_fields(state, required, 32, ["net_tick", "round_id", "countdown", "effects"]):
		return false
	if not _snapshot_int(state.version, 3, 3) or not state.phase is String or state.phase not in ["playing", "finished"]:
		return false
	if not _snapshot_int(state.tick, 0, 1000000000) or not _snapshot_int(state.spawned_bricks, 0, 999999957) or not _snapshot_int(state.next_brick_id, 43, 1000000000) or state.next_brick_id != 43 + state.spawned_bricks:
		return false
	if not _snapshot_number(state.elapsed, 0.0, 1000000000.0 * FIXED_DT) or absf(float(state.elapsed) - float(state.tick) * FIXED_DT) > 0.001:
		return false
	if not _snapshot_number(state.serveTimer, 0.0, 1.0) or not _snapshot_number(state.flow_timer, 0.0, 1.251):
		return false
	if not _snapshot_number(state.accumulator, 0.0, 3600.0) or not _snapshot_number(state.drop_chance, 0.0, 1.0):
		return false
	if not state.seed is int or not state.rng_state is int or not _snapshot_int(state.next_ball_id, 3, 1000000000) or not _snapshot_int(state.next_drop_id, 1, 1000000000):
		return false
	if not state.players is Array or state.players.size() != 2:
		return false
	for player in state.players:
		if not player is Dictionary or not _snapshot_fields(player, ["x", "hp", "wide", "shield", "overdrive", "sticky", "score", "velocity", "aim"], 12):
			return false
		if not _snapshot_int(player.hp, 0, 5) or not _snapshot_int(player.shield, 0, 3) or not _snapshot_int(player.score, 0, 2000000000):
			return false
		if not _snapshot_number(player.aim, -1.0, 1.0):
			return false
		if not _snapshot_number(player.sticky, 0.0, 12.001) or not _snapshot_number(player.wide, 0.0, 12.001) or not _snapshot_number(player.overdrive, 0.0, 8.001) or not _snapshot_number(player.velocity, -42000.0, 42000.0):
			return false
		var half: float = WIDE_WIDTH * 0.5 if float(player.wide) > 0.0 else PADDLE_WIDTH * 0.5
		if not _snapshot_number(player.x, LEFT + half - 0.02, RIGHT - half + 0.02):
			return false
	if not _snapshot_int(state.winner, -1, 1):
		return false
	if state.phase == "playing":
		if state.winner != -1 or state.players[0].hp == 0 or state.players[1].hp == 0:
			return false
	elif float(state.elapsed) < MATCH_SECONDS - 0.01:
		if state.winner == -1:
			if state.players[0].hp != 0 or state.players[1].hp != 0: return false
		elif state.players[1 - state.winner].hp != 0 or state.players[state.winner].hp <= 0:
			return false
	if not state.balls is Array or state.balls.size() < 2 or state.balls.size() > MAX_BALLS:
		return false
	var ball_ids: Dictionary = {}
	var base_count: int = 0
	for ball in state.balls:
		if not ball is Dictionary or not _snapshot_fields(ball, ["id", "pos", "vel", "owner", "radius", "base", "serve", "speed", "overdrive", "damage", "hits", "held", "hold_timer", "hold_offset", "hold_kind"], 18):
			return false
		if not _snapshot_int(ball.id, 1, int(state.next_ball_id) - 1) or ball_ids.has(ball.id) or not _snapshot_int(ball.owner, 0, 1):
			return false
		ball_ids[ball.id] = true
		if not ball.held is bool or not ball.base is bool or not ball.overdrive is bool or not _snapshot_int(ball.hits, 0, 1000000000):
			return false
		if ball.base:
			base_count += 1
		if not _snapshot_number(ball.hold_timer, 0.0, 3.601) or not _snapshot_number(ball.hold_offset, -50.0, 50.0):
			return false
		if not ball.hold_kind is String or ball.hold_kind not in ["", "sticky", "bonus"]:
			return false
		if ball.held:
			var player: Dictionary = state.players[ball.owner]
			var half: float = (WIDE_WIDTH if float(player.wide) > 0.0 else PADDLE_WIDTH) * 0.5 - BALL_RADIUS
			var direction: float = -1.0 if ball.owner == 0 else 1.0
			var expected: Vector2 = Vector2(float(player.x) + float(ball.hold_offset), float(PADDLE_Y[ball.owner]) + direction * (PADDLE_HEIGHT * 0.5 + BALL_RADIUS + 0.03))
			if ball.hold_kind == "" or (ball.hold_kind == "bonus" and ball.base) or not _snapshot_number(ball.serve, 0.0, 0.0) or float(ball.hold_timer) <= 0.0 or absf(float(ball.hold_offset)) > half + 0.001:
				return false
			if not ball.pos is Vector2 or Vector2(ball.pos).distance_to(expected) > 0.05:
				return false
		elif ball.hold_kind != "" or float(ball.hold_timer) != 0.0 or float(ball.hold_offset) != 0.0:
			return false
		if not _snapshot_number(ball.radius, BALL_RADIUS, BALL_RADIUS) or not _snapshot_number(ball.serve, 0.0, 1.0) or not _snapshot_number(ball.speed, 180.0, 400.0):
			return false
		if not _snapshot_int(ball.damage, 1, 2) or ball.damage != (2 if ball.overdrive else 1):
			return false
		if not _snapshot_vector(ball.pos, LEFT + BALL_RADIUS - 0.1, RIGHT - BALL_RADIUS + 0.1, TOP_GOAL - 0.1, BOTTOM_GOAL + 0.1):
			return false
		if not _snapshot_vector(ball.vel, -400.1, 400.1, -400.1, 400.1):
			return false
		var speed: float = Vector2(ball.vel).length()
		if speed < 179.9 or speed > 400.1:
			return false
	if base_count != 2:
		return false
	if not state.bricks is Array or state.bricks.size() > 42:
		return false
	if not state.feeders is Array or state.feeders.size() != 2:
		return false
	var reserved_rows: Dictionary = {}
	for index in range(2):
		var feeder = state.feeders[index]
		if not feeder is Dictionary or not _snapshot_fields(feeder, ["side", "y", "row", "phase", "cooldown", "progress", "speed"], 10):
			return false
		if not _snapshot_int(feeder.side, -1, 1) or feeder.side != (-1 if index == 0 else 1) or not _snapshot_int(feeder.row, -1, 5):
			return false
		if not feeder.phase is String or feeder.phase not in ["wait", "travel", "push"]:
			return false
		if not _snapshot_number(feeder.y, 270.0, 370.0) or not _snapshot_number(feeder.cooldown, 0.0, 0.701) or not _snapshot_number(feeder.progress, 0.0, 1.0) or not _snapshot_number(feeder.speed, 0.0, 36.0):
			return false
		if feeder.phase == "wait":
			if feeder.row != -1 or feeder.progress != 0.0 or feeder.speed != 0.0:
				return false
		else:
			if feeder.row < 0 or reserved_rows.has(feeder.row) or feeder.cooldown > EPS:
				return false
			reserved_rows[feeder.row] = feeder
			if feeder.phase == "travel":
				if feeder.progress != 0.0 or feeder.speed != 0.0:
					return false
			elif absf(float(feeder.y) - (270.0 + feeder.row * 20.0)) > 0.001 or feeder.speed not in [26.0, 36.0] or feeder.progress >= 1.0:
				return false
	var brick_ids: Dictionary = {}
	var slots: Dictionary = {}
	for brick in state.bricks:
		if not brick is Dictionary or not _snapshot_fields(brick, ["id", "rect", "hp", "max_hp", "row", "column", "origin_side", "target_x", "moving", "speed", "style"], 14):
			return false
		if not _snapshot_int(brick.row, 0, 5) or not _snapshot_int(brick.column, 0, 6) or not _snapshot_int(brick.hp, 1, 3) or not _snapshot_int(brick.max_hp, 1, 3):
			return false
		if not _snapshot_int(brick.id, 1, int(state.next_brick_id) - 1) or brick_ids.has(brick.id) or brick.hp > brick.max_hp:
			return false
		var slot: int = int(brick.row) * 7 + int(brick.column)
		if slots.has(slot) or not brick.moving is bool or not _snapshot_int(brick.origin_side, -1, 1) or brick.origin_side == 0:
			return false
		if not _snapshot_int(brick.style, 0, 2) or brick.style != brick.max_hp - 1:
			return false
		var target: float = BRICK_ORIGIN.x + float(brick.column) * 41.0
		if not _snapshot_number(brick.target_x, target, target) or not _snapshot_number(brick.speed, 0.0, 36.02):
			return false
		if not brick.rect is Rect2 or brick.rect.size != Vector2(36, 14) or not _snapshot_vector(brick.rect.position, -2.0, 326.0, BRICK_ORIGIN.y + float(brick.row) * 20.0, BRICK_ORIGIN.y + float(brick.row) * 20.0):
			return false
		if brick.moving:
			if not reserved_rows.has(brick.row):
				return false
			var feeder: Dictionary = reserved_rows[brick.row]
			var expected: float = target + int(feeder.side) * 41.0 * (1.0 - float(feeder.progress))
			if feeder.phase != "push" or brick.origin_side != feeder.side or float(brick.speed) <= 0.0 or absf(brick.rect.position.x - expected) > 0.002:
				return false
			if (feeder.side < 0 and brick.column > 3) or (feeder.side > 0 and brick.column < 3):
				return false
		elif absf(float(brick.rect.position.x) - target) > 0.001:
			return false
		brick_ids[brick.id] = true
		slots[slot] = true
	# Every brick remains separated, including translating chains and center reservations.
	for index in range(state.bricks.size()):
		for other_index in range(index + 1, state.bricks.size()):
			if state.bricks[index].rect.intersects(state.bricks[other_index].rect):
				return false
	if not state.drops is Array or state.drops.size() > 256:
		return false
	var drop_ids: Dictionary = {}
	for drop in state.drops:
		if not drop is Dictionary or not _snapshot_fields(drop, ["id", "pos", "owner", "kind"], 8):
			return false
		if not _snapshot_int(drop.id, 1, int(state.next_drop_id) - 1) or drop_ids.has(drop.id) or not _snapshot_int(drop.owner, 0, 1):
			return false
		drop_ids[drop.id] = true
		if not drop.kind is String or drop.kind not in POWER_KINDS or not _snapshot_vector(drop.pos, LEFT, RIGHT, TOP_GOAL, BOTTOM_GOAL):
			return false
	if state.has("net_tick") and not _snapshot_int(state.net_tick, 0, 1000000000):
		return false
	if state.has("round_id") and not _snapshot_int(state.round_id, 0, 2147483647):
		return false
	if state.has("countdown") and not _snapshot_number(state.countdown, 0.0, 3.0):
		return false
	if state.has("effects"):
		if not state.effects is Array or state.effects.size() > 24:
			return false
		var previous_id: int = 0
		for record in state.effects:
			if not record is Dictionary or not _snapshot_fields(record, ["id", "event"], 4) or not _snapshot_int(record.id, previous_id + 1, 1000000000):
				return false
			previous_id = record.id
			if not record.event is Dictionary or not _snapshot_event(record.event, state):
				return false
	# Reserved fields above are checked structurally; permit only bounded benign metadata.
	var known: Array = required + ["net_tick", "round_id", "countdown", "effects"]
	for key in state:
		if str(key) not in known and not _snapshot_metadata(state[key], 0):
			return false
	return true


static func _snapshot_int(value, lower: int, upper: int) -> bool:
	return value is int and value >= lower and value <= upper


static func _snapshot_number(value, lower: float, upper: float) -> bool:
	return (value is int or value is float) and is_finite(float(value)) and float(value) >= lower and float(value) <= upper


static func _snapshot_vector(value, left: float, right: float, top: float, bottom: float) -> bool:
	return value is Vector2 and _snapshot_number(value.x, left, right) and _snapshot_number(value.y, top, bottom)


static func _snapshot_fields(value: Dictionary, required: Array, maximum: int, optional: Array = []) -> bool:
	if value.size() > maximum:
		return false
	for key in required:
		if not value.has(key):
			return false
	for key in value:
		# Godot 4.7 uses StringName for keys introduced by dot assignment.
		# Both representations are text keys; retain the same resource bounds.
		if not (key is String or key is StringName) or str(key).length() > 48:
			return false
		if str(key) not in required and str(key) not in optional and not _snapshot_metadata(value[key], 0):
			return false
	return true


static func _snapshot_metadata(value, depth: int) -> bool:
	if depth > 3:
		return false
	if value == null or value is bool or value is int:
		return true
	if value is float:
		return is_finite(value)
	if value is String:
		return value.length() <= 256
	if value is Vector2:
		return _snapshot_vector(value, -1000000, 1000000, -1000000, 1000000)
	if value is Rect2:
		return _snapshot_vector(value.position, -1000000, 1000000, -1000000, 1000000) and _snapshot_vector(value.size, 0, 1000000, 0, 1000000)
	if value is Array:
		if value.size() > 16:
			return false
		for item in value:
			if not _snapshot_metadata(item, depth + 1):
				return false
		return true
	if value is Dictionary:
		if value.size() > 16:
			return false
		for key in value:
			if not (key is String or key is StringName) or str(key).length() > 48 or not _snapshot_metadata(value[key], depth + 1):
				return false
		return true
	return false


static func _snapshot_event(event: Dictionary, state: Dictionary) -> bool:
	if not _snapshot_fields(event, ["type"], 10) or not event.type is String:
		return false
	var requirements: Dictionary = {
		"serve": ["ball", "owner", "pos"], "paddle_hit": ["ball", "owner", "pos"],
		"wall_hit": ["ball", "pos"], "brick_hit": ["brick", "owner", "pos", "hp"],
		"brick_destroyed": ["brick", "owner", "pos"], "drop_spawned": ["id", "owner", "kind", "pos"],
		"powerup_capped": ["player", "owner", "kind", "pos"], "powerup": ["player", "owner", "kind", "pos"], "goal": ["player", "defender", "owner", "ball", "hp", "pos"],
		"shield_block": ["player", "ball", "pos"], "sticky_catch": ["ball", "owner", "pos"],
		"sticky_release": ["ball", "owner", "pos"], "brick_spawned": ["brick", "side", "pos"],
		"brick_arrived": ["brick", "side", "pos"], "game_over": ["winner"]}
	if not requirements.has(event.type):
		return false
	for key in requirements[event.type]:
		if not event.has(key):
			return false
	for key in ["owner", "player", "defender", "winner"]:
		if event.has(key) and not _snapshot_int(event[key], -1 if key == "winner" else 0, 1):
			return false
	if event.has("pos") and not _snapshot_vector(event.pos, -36.0, 396.0, TOP_GOAL - 1, BOTTOM_GOAL + 1):
		return false
	if event.has("ball") and not _snapshot_int(event.ball, 1, int(state.next_ball_id) - 1):
		return false
	if event.has("brick") and not _snapshot_int(event.brick, 1, int(state.next_brick_id) - 1):
		return false
	if event.has("id") and not _snapshot_int(event.id, 1, int(state.next_drop_id) - 1):
		return false
	if event.has("hp") and not _snapshot_int(event.hp, 0, 5):
		return false
	if event.has("side") and (not _snapshot_int(event.side, -1, 1) or event.side == 0):
		return false
	if event.has("kind") and (not event.kind is String or event.kind not in POWER_KINDS):
		return false
	return true


func apply_snapshot(data: Dictionary) -> void:
	# Rejection is atomic even when called outside the network adapter.
	if not valid_snapshot(data):
		return
	feeders = data["feeders"].duplicate(true)
	players = data["players"].duplicate(true)
	balls = data["balls"].duplicate(true)
	bricks = data["bricks"].duplicate(true)
	drops = data["drops"].duplicate(true)
	elapsed = float(data["elapsed"])
	phase = String(data["phase"])
	winner = int(data["winner"])
	spawned_bricks = int(data["spawned_bricks"])
	_next_brick_id = int(data["next_brick_id"])
	serveTimer = float(data["serveTimer"])
	flow_timer = float(data["flow_timer"])
	tick = int(data["tick"])
	_accumulator = float(data["accumulator"])
	_seed_value = int(data["seed"])
	_rng.seed = _seed_value
	_rng.state = int(data["rng_state"])
	_next_ball_id = int(data["next_ball_id"])
	_next_drop_id = int(data["next_drop_id"])
	drop_chance = float(data["drop_chance"])
	events.clear()
