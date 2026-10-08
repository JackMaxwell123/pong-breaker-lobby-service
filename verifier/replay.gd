extends SceneTree
## Server-owned replay reader. Never evaluate code or deserialize native objects from a player.

func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var replay_path := ""
	var simulation_path := ""
	for i in range(args.size() - 1):
		if args[i] == "--replay": replay_path = args[i + 1]
		if args[i] == "--simulation": simulation_path = args[i + 1]
	if replay_path.is_empty() or simulation_path.is_empty():
		quit(2)
		return
	var file := FileAccess.open(replay_path, FileAccess.READ)
	if file == null or file.get_length() > 12000000:
		quit(2)
		return
	var replay = JSON.parse_string(file.get_as_text())
	if not replay is Dictionary or replay.get("version") != 1 or not replay.get("frames") is Array:
		quit(2)
		return
	var frames: Array = replay.frames
	if frames.size() < 1 or frames.size() > 54000:
		quit(2)
		return
	var script = load(simulation_path)
	if script == null:
		quit(2)
		return
	var sim = script.new()
	sim.reset(int(replay.seed))
	for index in range(frames.size()):
		var frame = frames[index]
		if not frame is Array or frame.size() != 6 or sim.phase != "playing":
			quit(3)
			return
		var targets := [float(frame[0]) / 1000.0, float(frame[1]) / 1000.0]
		var aims := [float(frame[2]) / 1000.0, float(frame[3]) / 1000.0]
		sim.set_aim(0, aims[0])
		sim.set_aim(1, aims[1])
		sim.step(1.0 / 60.0, targets)
		# Matches the actual client caller order: releases happen after this frame's step.
		for player in range(2):
			if int(frame[4 + player]) == 1:
				sim.release_sticky(player, aims[player])
	if (sim.phase != "finished" and not replay.get("allow_incomplete", false)) or sim.winner not in [-1, 0, 1]:
		quit(3)
		return
	print("PB_VERIFIED:" + JSON.stringify({"phase": sim.phase, "winner": sim.winner, "frames": frames.size(), "ticks": sim.tick}))
	quit(0)
