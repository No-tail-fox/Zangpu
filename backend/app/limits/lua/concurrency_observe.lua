local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now_ms)
local count = redis.call("ZCARD", KEYS[1])

if count == 0 then
    redis.call("DEL", KEYS[1])
    return {0, now_ms, now_ms, now_ms}
end

local earliest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
local latest = redis.call("ZREVRANGE", KEYS[1], 0, 0, "WITHSCORES")
return {
    count,
    math.floor(tonumber(earliest[2])),
    math.floor(tonumber(latest[2])),
    now_ms
}
