from torch import nn
import torch
import ipdb

# Loss functions.
def pytorch_quantile_loss(y, y_pred, quantile):
  """Computes quantile loss for pytorch.
  Standard quantile loss as defined in the "Training Procedure" section of
  the main TFT paper
  Args:
    y: Targets
    y_pred: Predictions
    quantile: Quantile to use for loss calculations (between 0 & 1)
  Returns:
    Tensor for quantile loss.
  """

  # Checks quantile
  if quantile < 0 or quantile > 1:
    raise ValueError(
        'Illegal quantile value={}! Values should be between 0 and 1.'.format(
            quantile))

  prediction_underflow = y - y_pred
  q_loss = quantile * torch.max(prediction_underflow, 0.) + (1. - quantile) * torch.max(-prediction_underflow, 0.)

  return torch.sum(q_loss, axis=-1)

class TimeDistributed(nn.Module):
    def __init__(self, module, batch_first=False):
        super(TimeDistributed, self).__init__()
        self.module = module
        self.batch_first = batch_first

    def forward(self, x):

        if len(x.size()) <= 2:
            return self.module(x)

        # Squash samples and timesteps into a single axis
        x_reshape = x.contiguous().view(-1, x.size(-1))  # (samples * timesteps, input_size)

        y = self.module(x_reshape)

        # We have to reshape Y
        if self.batch_first:
            y = y.contiguous().view(x.size(0), -1, y.size(-1))  # (samples, timesteps, output_size)
        else:
            y = y.view(-1, x.size(1), y.size(-1))  # (timesteps, samples, output_size)

        return y

class GLU(nn.Module):
    def __init__(self, input_size):
        super(GLU, self).__init__()
        
        self.fc1 = nn.Linear(input_size,input_size)
        self.fc2 = nn.Linear(input_size, input_size)
        self.sigmoid = nn.Sigmoid()
        
    def forward(self, x):
        
        sig = self.sigmoid(self.fc1(x))
        x = self.fc2(x)
        return torch.mul(sig, x)



class GatedResidualNetwork(nn.Module):
    def __init__(self, input_size, output_size, dropout, hidden_context_size=None):
        super(GatedResidualNetwork, self).__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.hidden_context_size = hidden_context_size
        self.dropout = dropout
        
        self.fc1 = TimeDistributed(nn.Linear(self.input_size, self.output_size))
        self.elu1 = nn.ELU()
        
        if self.hidden_context_size is not None:
            self.context = TimeDistributed(nn.Linear(self.hidden_context_size, self.output_size))
            
        self.fc2 = TimeDistributed(nn.Linear(self.input_size,  self.output_size))
        self.elu2 = nn.ELU()
        
        self.dropout = nn.Dropout(self.dropout)
        self.bn = TimeDistributed(nn.BatchNorm1d(self.input_size))
        self.gate = TimeDistributed(GLU(self.input_size))
    def forward(self, x, context=None):
        residual = x
        
        x = self.fc1(x)
        if context is not None:
            context = self.context(context)
            x = x+context
        x = self.elu1(x)
        
        x = self.fc2(x)
        x = self.dropout(x)
        x = self.gate(x)
        x = x+residual
        x = self.bn(x)
        
        return x

class TFT(nn.Module):
    def __init__(self, config):
        #config-embedding_vocab_sizes is list of vocabs
        #
        super(TFT, self).__init__()
        self.device = config['device']
        self.batch_size=config['batch_size']
        self.static_variables = config['static_variables']
        self.encode_length = config['encode_length']
        self.time_varying_categoical_variables =  config['time_varying_categoical_variables']
        self.time_varying_real_variables =  config['time_varying_real_variables']
        self.hidden_size = config['lstm_hidden_dimension']
        self.lstm_layers = config['lstm_layers']
        self.dropout = config['dropout']
        self.attn_heads = config['attn_heads']
        
        self.static_embedding_layers = []
        for i in range(self.static_variables):
            emb = nn.Embedding(config['static_embedding_vocab_sizes'][i], config['embedding_dim']).to(self.device)
            self.static_embedding_layers.append(emb)
        
        
        
        self.time_varying_embedding_layers = []
        for i in range(self.time_varying_categoical_variables):
            emb = TimeDistributed(nn.Embedding(config['time_varying_embedding_vocab_sizes'][i], config['embedding_dim']), batch_first=True).to(self.device)
            self.time_varying_embedding_layers.append(emb)
            
        self.time_varying_linear_layers = []
        for i in range(self.time_varying_real_variables):
            emb = TimeDistributed(nn.Linear(1, config['embedding_dim']), batch_first=True).to(self.device)
            self.time_varying_linear_layers.append(emb)
        
        self.lstm_input_size = config['embedding_dim']*(config['time_varying_real_variables'] +  
                                                        config['time_varying_categoical_variables'] +
                                                        config['static_variables'])
                                                        
        self.lstm_encoder = nn.LSTM(input_size=self.lstm_input_size, 
                            hidden_size=self.hidden_size,
                           num_layers=self.lstm_layers,
                           dropout=config['dropout'])
        
        self.lstm_decoder = nn.LSTM(input_size=self.lstm_input_size,
                                   hidden_size=self.hidden_size,
                                   num_layers=self.lstm_layers,
                                   dropout=config['dropout'])
        
        self.static_enrichment = GatedResidualNetwork(self.hidden_size, self.hidden_size, self.dropout, config['embedding_dim']*self.static_variables)
        
        self.pos_wise_ff = TimeDistributed(GatedResidualNetwork(self.hidden_size, self.hidden_size, self.dropout))
        
        
        self.multihead_attn = nn.MultiheadAttention(self.hidden_size, self.attn_heads)
        
        self.output_layer = TimeDistributed(nn.Linear(self.hidden_size, 1), batch_first=True)
        
    def init_hidden(self):
        return torch.zeros(self.lstm_layers, self.batch_size, self.hidden_size, device=self.device)
        
    def apply_embedding(self, x, static_embedding):
        ###x should have dimensions (batch_size, timesteps, input_size)
        #Time-varying real embeddings 
        time_varying_real_vectors = []
        for i in range(self.time_varying_real_variables):
            emb = self.time_varying_linear_layers[i](x[:,:,i].view(x.size(0), -1, 1))
            time_varying_real_vectors.append(emb)
        time_varying_real_embedding = torch.cat(time_varying_real_vectors, dim=2)
        
         ##Time-varying categorical embeddings (ie hour)
        time_varying_categoical_vectors = []
        for i in range(self.time_varying_categoical_variables):
            emb = self.time_varying_embedding_layers[i](x[:, :,self.time_varying_real_variables+i].view(x.size(0), -1, 1).long())
            time_varying_categoical_vectors.append(emb)
        time_varying_categoical_embedding = torch.cat(time_varying_categoical_vectors, dim=2)  

        ##repeat static_embedding for all timesteps
        static_embedding = torch.cat(time_varying_categoical_embedding.size(1)*[static_embedding])
        static_embedding = static_embedding.view(time_varying_categoical_embedding.size(0),time_varying_categoical_embedding.size(1),-1 )
        
        ##concatenate all embeddings
        embeddings = torch.cat([static_embedding,time_varying_categoical_embedding,time_varying_real_embedding], dim=2)
        
        return embeddings.view(-1,x.size(0),embeddings.size(2))
    
    def encode(self, x, hidden=None):
    
        if hidden is None:
            hidden = self.init_hidden()
            
        output, (hidden, cell) = self.lstm_encoder(x, (hidden, hidden))
        
        return output, hidden
        
    def decode(self, x, hidden=None):
        
        if hidden is None:
            hidden = self.init_hidden()
            
        output, (hidden, cell) = self.lstm_decoder(x, (hidden,hidden))
        
        return output, hidden
    
    def _generate_square_subsequent_mask(self, sz):
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0)).to(self.device)
        return mask
    
    def forward(self, x):
        ##inputs should be [batch_num, time_steps, inputs]
        ##input dimension should be
            # static
            # time_varying_categorical
            # time_varying_real
        embedding_vectors = []
        for i in range(self.static_variables):
            #only need static variable from the first timestep
            emb = self.static_embedding_layers[i](x['identifier'][:,0, i].long().to(self.device))
            embedding_vectors.append(emb)
        static_embedding = torch.cat(embedding_vectors, dim=1)

        embeddings_encoder = self.apply_embedding(x['inputs'][:,:self.encode_length,:].float().to(self.device), static_embedding)
        embeddings_decoder = self.apply_embedding(x['inputs'][:,self.encode_length:,:].float().to(self.device), static_embedding)

        encoder_output, hidden = self.encode(embeddings_encoder)
        decoder_output, _ = self.decode(embeddings_decoder, hidden)
        lstm_output = torch.cat([encoder_output, decoder_output], dim=0)
        static_embedding = torch.cat(lstm_output.size(0)*[static_embedding]).view(lstm_output.size(0), lstm_output.size(1), -1)
        attn_input = self.static_enrichment(lstm_output, static_embedding)
        
        mask = self._generate_square_subsequent_mask(attn_input.size(0))
        
        attn_output, attn_output_weights = self.multihead_attn(attn_input, attn_input, lstm_output,attn_mask=mask)
        attn_output = attn_output[self.encode_length:,:,:]
        output = self.output_layer(attn_output.view(self.batch_size, -1, self.hidden_size))
        
        
        
        return  output,encoder_output, decoder_output, attn_output, attn_output_weights
    
        
        