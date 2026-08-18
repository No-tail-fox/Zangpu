local removed = redis.call("ZREM", KEYS[1], ARGV[1])
redis.call("ZREM", KEYS[2], ARGV[1])
redis.call("ZREM", KEYS[3], ARGV[1])
redis.call("ZREM", KEYS[4], ARGV[1])

for _, key in ipairs(KEYS) do
    if redis.call("ZCARD", key) == 0 then
        redis.call("DEL", key)
    end
end

return removed
