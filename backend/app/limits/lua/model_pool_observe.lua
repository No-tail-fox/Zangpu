local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now_ms)
local expired = redis.call("ZRANGEBYSCORE", KEYS[3], "-inf", now_ms)
for _, expired_member in ipairs(expired) do
    redis.call("ZREM", KEYS[2], expired_member)
    redis.call("ZREM", KEYS[3], expired_member)
end
redis.call("ZREMRANGEBYSCORE", KEYS[4], "-inf", now_ms)

local active_count = redis.call("ZCARD", KEYS[1])
local queue_count = redis.call("ZCARD", KEYS[4])
local pool_queue_count = redis.call("ZCARD", KEYS[2])
local active_expiry = now_ms
local queue_expiry = now_ms

if active_count > 0 then
    local earliest_active = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
    active_expiry = math.floor(tonumber(earliest_active[2]))
else
    redis.call("DEL", KEYS[1])
end

if pool_queue_count > 0 then
    local earliest_queue = redis.call("ZRANGE", KEYS[3], 0, 0, "WITHSCORES")
    queue_expiry = math.floor(tonumber(earliest_queue[2]))
else
    redis.call("DEL", KEYS[2])
    redis.call("DEL", KEYS[3])
end

if queue_count == 0 then
    redis.call("DEL", KEYS[4])
end

return {active_count, queue_count, pool_queue_count, active_expiry, queue_expiry, now_ms}
