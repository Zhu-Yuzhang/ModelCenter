import torch
from model_center.layer import Encoder, Decoder, Embedding, Linear, SegmentPositionEmbedding 
from model_center.layer import LayerNorm
from config import CPM3Config

class CPM3(torch.nn.Module):
    
    def __init__(self, config: CPM3Config):
        
        super().__init__()

        self.encoder = Encoder(
            num_layers = config.num_layers,
            dim_model = config.dim_model, 
            dim_ff = config.dim_ff,
            num_heads = config.num_heads,
            dim_head = config.dim_head,
            dtype = config.dtype, 
            int8 = config.int8,
            norm_eps = config.norm_eps, 
            norm_init_var = config.norm_init_var,
            norm_bias = config.norm_bias,
            att_init_mean = config.att_init_mean, 
            att_init_std = config.att_init_std,
            att_bias = config.att_bias,
            att_mask_value = float(config.att_mask_value),
            pos_bias_type = config.pos_bias_type,
            ffn_init_mean = config.ffn_init_mean, 
            ffn_init_std = config.ffn_init_std,
            ffn_bias = config.ffn_bias,
            ffn_activate_fn = config.ffn_activate_fn,
            length_scale = config.length_scale,
            attn_scale = config.attn_scale,
            dropout_p = config.dropout_p,
            use_cache = config.use_cache)
        self.cached_attn_mask_pos_bias = None
        self.prompt_embedding = Embedding(
            vocab_size = config.prompt_types * config.prompt_length, 
            embedding_size = config.dim_model,
            length_scale = config.length_scale,
            dtype = config.dtype,
            int8 = config.int8,
            init_mean = config.emb_init_mean,
            init_std = config.emb_init_std,)

        self.input_embedding = Embedding(
            vocab_size = config.vocab_size, 
            embedding_size = config.dim_model,
            length_scale = config.length_scale,
            dtype = config.dtype,
            int8 = config.int8,
            init_mean = config.emb_init_mean,
            init_std = config.emb_init_std,)

        self.position_bias = SegmentPositionEmbedding(
            num_segments = config.segment_types,
            num_heads = config.num_heads, 
            num_buckets = config.position_bias_num_buckets, 
            max_distance = config.position_bias_max_distance, 
            absolute_inner_segment = config.absolute_inner_segment,
            bidirectional = True,
            dtype = config.dtype,)
        self.past_key_value = None
        self.prompt_length = config.prompt_length
        self.tied = config.tied
        self.cls_head = config.cls_head
        if self.cls_head:
            self.output_projection = Linear(
                dim_out = config.cls_head,
                dim_in = config.dim_model,
                length_scale = config.length_scale,
                dtype = config.dtype,
                int8 = config.int8,
                init_mean = config.proj_init_mean,
                init_std = config.proj_init_std,
                bias = config.proj_bias,)
        elif not self.tied:
            self.output_projection = Linear(
                dim_out = config.vocab_size,
                dim_in = config.dim_model,
                length_scale = config.length_scale,
                dtype = config.dtype,
                int8 = config.int8,
                init_mean = config.proj_init_mean,
                init_std = config.proj_init_std,
                bias = config.proj_bias,)

    def forward(self, input : torch.Tensor, # (batch, seqlen)
                      length : torch.Tensor, # (batch)
                      context : torch.Tensor, # (batch, seqlen)
                      position: torch.Tensor, # (batch, seqlen)
                      segment: torch.Tensor, # (batch, seqlen)
                      span : torch.Tensor,  # (batch, seqlen)
                      use_cache = False,
                    ):
        # when enable use_cache ,the batch size need to be 1
        batch = input.size(0)
        context_ = context[0].clone()
        context_[context_.nonzero()[-1][0]+1:] = True
        context_ = context_.logical_not()
        right_ctx_start_idx = context_.nonzero()[-1][0] + 1
        last_input_idx = context_.nonzero()[0][0] - 1
        if self.past_key_value is None:
            past_length = 0

            input_prompt = input[:, :self.prompt_length].contiguous()
            input_ids = input[:, self.prompt_length:].contiguous()

            prompt_states = self.prompt_embedding(input_prompt)
            hidden_states = self.input_embedding(input_ids)
            hidden_states = torch.cat([prompt_states, hidden_states], 1)
            
            if use_cache:
                input_seqlen = input.size(1) - (right_ctx_start_idx - last_input_idx) + 1
                self.past_key_value = tuple([None] * self.encoder.num_layers)
        else:
            past_length = self.past_key_value[0][0].size(-2)
            hidden_states = self.input_embedding(input)
            input_seqlen = input.size(1)


        if self.cached_attn_mask_pos_bias is None:
            with torch.no_grad():
                device = input.device
            seqlen = context.size(1)
            directional_mask_2d = torch.arange(seqlen, device=device) <= torch.arange(seqlen, device=device).view(-1, 1)
            attention_mask = context[:, None, :] | (context[:, :, None].logical_not() & directional_mask_2d.view(1, seqlen, seqlen))
            attention_mask = attention_mask & (span[:, None, :] == span[:, :, None])
            mask_1d = torch.arange(seqlen, device=device)[None, :].repeat(batch, 1) < length[:, None]
            attention_mask = mask_1d.view(batch, seqlen, 1) & mask_1d.view(batch, 1, seqlen) & attention_mask

            position_bias = self.position_bias(position, position, segment, segment)
            if use_cache:
                assert attention_mask.size(1) == attention_mask.size(2) == position_bias.size(2) == position_bias.size(3)
                switch_indices = [x for x in range(right_ctx_start_idx, attention_mask.size(1))] + [x for x in range(right_ctx_start_idx)]
                attention_mask = attention_mask[:, switch_indices, :]
                attention_mask = attention_mask[:, :, switch_indices]
                position_bias = position_bias[:, :, switch_indices, :]
                position_bias = position_bias[:, :, :, switch_indices]
                self.cached_attn_mask_pos_bias = (attention_mask, position_bias)
        else:
            attention_mask, position_bias = self.cached_attn_mask_pos_bias
        if use_cache:
            valid_len = input_seqlen + past_length
            if past_length == 0:
                assert hidden_states.size(1) == attention_mask.size(1)
                hidden_states = hidden_states[:, switch_indices, :]
                hidden_states = hidden_states[:, :valid_len, :]

                attention_mask = attention_mask[:, :valid_len, :valid_len]
                position_bias = position_bias[:, :, :valid_len, :valid_len]
            else:
                attention_mask = attention_mask[:, past_length:valid_len, :valid_len]
                position_bias = position_bias[:, :, past_length:valid_len, :valid_len]
            hidden_states,current_key_values = self.encoder(hidden_states, attention_mask, position_bias, self.past_key_value, use_cache=True)
            self.past_key_value = current_key_values
        else:
            hidden_states = self.encoder(hidden_states, attention_mask, position_bias, self.past_key_value,use_cache=False)

        if self.cls_head:
            logits = self.output_projection(hidden_states)
        elif not self.tied:
            logits = self.output_projection(hidden_states)
        else:
            logits = self.input_embedding.projection(hidden_states)
        return logits, hidden_states

